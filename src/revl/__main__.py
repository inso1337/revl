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

        # First-class launder (G8 item 24): a host extern reached ONLY as a
        # value handed to a dispatcher — `indirect(ship, a)` — is named in no
        # call position, so the walk above never sees it. It is exactly the
        # reach the G4 fixed point already tracks to keep the read-only hint
        # sound: `_calls_in`'s value channel records the escaping callable, and
        # `fn_caps_map` says which boundaries that value carries. Fold that same
        # first-class reach onto the surface so a laundered `host:` crossing is
        # enumerated identically to a direct call (audit --diff can then see it).
        from .emission_analysis import _calls_in  # noqa: PLC0415 — lazy, like plan
        value_refs: set = set()
        _calls_in(comp.get("body") or [], set(), values=value_refs)
        for ref in value_refs:
            ref_caps = fn_caps_map.get(ref) or set()
            host |= {c for c in ref_caps if c != "*"}
            if "*" in ref_caps:
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
        # operator capabilities (docs/operator-capabilities.md, item 55): bind
        # the served session to one operator identity, so its management verbs
        # are scoped by that operator's grants. No profile => ungated (today's
        # root-over-transport), so this is opt-in for networked/multi-operator
        # use.
        if getattr(args, "operator_profile", None):
            from .mcp.operator import ProfileError, load_profile
            from .mcp.server import SESSION

            try:
                registry = load_profile(args.operator_profile)
            except (OSError, ProfileError) as error:
                print(f"error: cannot load operator profile "
                      f"{args.operator_profile}: {error}", file=sys.stderr)
                return 1
            token = getattr(args, "operator", None)
            operator = registry.get(token) if token else registry.sole()
            if operator is None:
                if token:
                    print(f"error: operator profile names no operator {token!r} "
                          f"(known: {', '.join(sorted(registry.operators)) or 'none'})",
                          file=sys.stderr)
                else:
                    print("error: the operator profile declares multiple "
                          "operators — pass --operator to select which identity "
                          "this session runs as", file=sys.stderr)
                return 1
            SESSION.operator = operator
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


def _run_export(args) -> int:
    """`revl export wit` — the reverse of `revl import wit` (docs/wit-bridge.md).

    Slice 1 of the Component Model bridge: pure IR codegen of the standard WIT
    interface a revl service or composition presents (the importer's type
    mapping, run backwards). No runtime, no emission, no binary — interface
    text only. Effects ride alongside the shape as `/// @revl:*` doc comments,
    because WIT's type system carries shape, not lifecycle.
    """
    from .export_wit import export_wit  # noqa: PLC0415

    try:
        ir = compile_files(args.files)
    except RevlError as error:
        if getattr(args, "json_diagnostics", False):
            print(json.dumps(report(error), indent=2))
        else:
            print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        source = export_wit(ir, service=args.service,
                            composition=args.composition, package=args.package)
    except RevlError as error:
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


def _run_undo(args) -> int:
    """`revl undo history.json [--to N]` — operator undo for a running system
    (roadmap item 65, docs/generation-history.md).

    A history document (`revl.generation-history`, produced by the session's
    `history_document()`) is a list of generation snapshots. This replays them
    into a *fresh* session — load the first, swap the rest — to reach the same
    live generation history, then performs the undo through the gate. `--to`
    names a recorded generation; omit it to undo to N−1. The undo is itself an
    admitted, gated change: a target the current checker rejects is refused and
    the composition is left untouched. Prints the dossier, tears down."""
    from .mcp import persist  # noqa: PLC0415
    from .mcp.session import Session, SessionError  # noqa: PLC0415

    try:
        with open(args.history, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        print(f"error: cannot read {args.history}: {error}", file=sys.stderr)
        return 1
    if not isinstance(doc, dict) or doc.get("kind") != "revl.generation-history":
        print(f"error: {args.history}: expected a revl.generation-history document "
              f"(from the session's history export)", file=sys.stderr)
        return 1
    gens = doc.get("generations") or []
    if len(gens) < 2:
        print("error: the history has fewer than two generations — nothing to "
              "undo to", file=sys.stderr)
        return 1

    # translate a recorded generation number to the position it will hold in the
    # replayed session (which numbers its generations 1..k as it boots them).
    to_session = None
    if args.to is not None:
        idx = next((i for i, g in enumerate(gens)
                    if g.get("generation") == args.to), None)
        if idx is None:
            recorded = [g.get("generation") for g in gens]
            print(f"error: generation {args.to} is not in the history document "
                  f"(recorded: {recorded})", file=sys.stderr)
            return 1
        to_session = idx + 1

    session = Session()
    try:
        for i, gen in enumerate(gens):
            snap = gen.get("snapshot")
            if not snap or not snap.get("sources"):
                print(f"error: generation {gen.get('generation')} has no "
                      f"re-admittable snapshot — the history cannot be replayed",
                      file=sys.stderr)
                if session.loaded:
                    session.unload()
                return 1
            ir = persist._recompile(snap["sources"])
            origin = persist._origin_from(snap["sources"])
            config = (snap.get("meta") or {}).get("config")
            if i == 0:
                session.load(ir, config, origin=origin)
            else:
                session.swap(ir, origin=origin)
        result = session.undo(to_session)
    except (SessionError, RevlError) as error:
        print(f"refused: {error}", file=sys.stderr)
        if session.loaded:
            session.unload()
        return 1

    _print_undo(result, args)
    unloaded = session.unload()
    if not getattr(args, "json", False):
        print(f"\ntorn down — no residue: {unloaded['noResidue']}")
    return 0 if result.get("undone") else 1


def _print_undo(result: dict, args) -> None:
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
        return
    dossier = result.get("dossier") or {}
    crossings = dossier.get("unemittableCrossings") or {}
    if not result.get("undone"):
        print(f"REFUSED: {result.get('reason')}")
        print("the running composition is untouched — an undo is a gated change.")
    else:
        print(f"undone: generation {dossier.get('fromGeneration')} -> "
              f"{result.get('toGeneration')} (now running as generation "
              f"{result.get('generation')}), re-admitted through the gate.")
    unloads = dossier.get("unloads") or []
    print(f"\nunloads: {', '.join(unloads) or '—'}")
    dropped = (dossier.get("stateDropped") or {}).get("provisions") or []
    print(f"state dropped (provisions withdrawn): "
          f"{', '.join(p['key'] for p in dropped) or '—'}")
    given_up = crossings.get("givenUp") or []
    print(f"\ninterim boundary crossings that NO undo can un-emit "
          f"(compensation is not inversion — §6.1):")
    for token in crossings.get("crossings") or []:
        mark = "  ! " if token in set(given_up) else "  ~ "
        note = "  (given up going forward, already exercised)" \
            if token in set(given_up) else "  (target still reaches this)"
        print(f"{mark}{token}{note}")
    if not (crossings.get("crossings") or []):
        print("  (none — the interim generations crossed no boundary)")


def _run_contract(args) -> int:
    """`revl contract` — federated contracts between sovereign compositions
    (docs/federation.md, roadmap item 58).

    `export` projects composition A's compiled IR into its consumer surface
    (the pinnable contract of what A requires from a provider). `check` runs a
    provider B's current manifest against a pinned surface through the same
    §5/drift predicate `revl version` uses (`version.diff_services`): a MAJOR
    drift is a contract break, and the gate exits nonzero naming it.
    """
    from .federation import check, consumer_surface, render

    if args.contract_command == "export":
        try:
            ir = compile_files(args.files)
        except RevlError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        surface = consumer_surface(ir, consumer=args.consumer)
        print(json.dumps(surface, indent=2))
        return 0

    # check: --consumer is a pinned surface artifact; --provider is either a
    # single compiled manifest .json or one/more .rvl sources compiled here.
    try:
        with open(args.consumer, encoding="utf-8") as handle:
            consumer_doc = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        print(f"error: cannot read {args.consumer}: {error}", file=sys.stderr)
        return 1

    provider_paths = list(args.provider)
    if len(provider_paths) == 1 and provider_paths[0].endswith(".json"):
        try:
            with open(provider_paths[0], encoding="utf-8") as handle:
                provider_ir = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            print(f"error: cannot read {provider_paths[0]}: {error}",
                  file=sys.stderr)
            return 1
        provider_label = provider_paths[0]
    else:
        try:
            provider_ir = compile_files(provider_paths)
        except RevlError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        provider_label = "the provider"

    try:
        result = check(consumer_doc, provider_ir)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(render(result, args.consumer, provider_label))
    return 0 if result["satisfied"] else 1


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


def _run_history_query(args) -> int:
    """`revl query {emitted-between,touched}` — the historical mode
    (docs/queries.md §9): the same query envelope answered against a RECORDED
    run rather than a static IR. Reads a replay-recording JSON and/or an
    item-27 lifecycle JSONL, never source."""
    from . import query, why_runtime  # noqa: PLC0415

    def _load_json(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    if args.query_command == "emitted-between":
        try:
            timeline = _load_json(args.timeline)
        except (OSError, ValueError) as error:
            print(f"error: cannot read timeline {args.timeline}: {error}",
                  file=sys.stderr)
            return 1
        result = query.emitted_between(timeline, args.frm, args.to,
                                       args.component)
    else:  # touched
        record = {}
        if args.timeline:
            try:
                record["timeline"] = _load_json(args.timeline)
            except (OSError, ValueError) as error:
                print(f"error: cannot read timeline {args.timeline}: {error}",
                      file=sys.stderr)
                return 1
        if args.trace:
            try:
                record["trace"] = why_runtime.read_trace(args.trace)
            except (OSError, ValueError) as error:
                print(f"error: cannot read trace {args.trace}: {error}",
                      file=sys.stderr)
                return 1
        if not record:
            print("error: give --trace and/or --timeline (a recorded run to "
                  "query)", file=sys.stderr)
            return 1
        result = query.lifetime(record, args.component)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(query.render(result))
    return 0 if result.get("ok") else 1


def _run_why(args) -> int:
    """`revl why <component> --trace run.jsonl` — the runtime companion to the
    compile-time why-traces: the cause chain behind a component's recorded
    lifecycle transition, and (with --check) the prediction-vs-actuality
    oracle (docs/why-runtime.md)."""
    from . import why_runtime

    try:
        trace = why_runtime.Trace.load(args.trace)
    except (OSError, ValueError) as error:
        print(f"error: cannot read trace {args.trace}: {error}", file=sys.stderr)
        return 1

    frames = trace.cause_chain(args.component)
    report = None
    if args.check is not None:
        try:
            ir = compile_files(args.check)
        except RevlError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        report = why_runtime.oracle(ir, args.component, trace)

    if args.json:
        payload = {
            "component": args.component,
            "chain": [
                {"component": f.component, "event": f.event,
                 "transition": f.transition, "cause": f.cause, "note": f.note}
                for f in frames
            ],
        }
        if report is not None:
            payload["oracle"] = report
        print(json.dumps(payload, indent=2))
    else:
        print(why_runtime.render_chain(args.component, frames))
        if report is not None:
            print("\n" + why_runtime.render_oracle(report))

    if report is not None and report.get("ok") and report.get("conforms") is False:
        return 1
    if not frames or (frames and frames[0].cause.get("kind") == "unrecorded"):
        return 1
    return 0


def _run_dash(args) -> int:
    """`revl dash <files...>` — the supervisor's cockpit (item 63). A READ-ONLY
    live view: the dependency graph (realms, seams), the causal trace, and the
    pending-decisions queue (widening acks, policy exceptions) with evidence.

    It sources everything from the read surfaces — `query` for the graph,
    `why_runtime` for the trace, `audit_diff`/`policy` for the queue — and
    mutates nothing. Live vs recorded is a matter of which optional inputs are
    given: a `--live-state` snapshot colors the graph as it stands now; a
    `--trace`/`--timeline` renders a recorded run with no runtime at all."""
    from . import dash, why_runtime  # noqa: PLC0415

    def _load_json(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    try:
        ir = compile_files(args.files)
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    trace = timeline = live_state = prev_audit = policy = None
    try:
        if args.trace:
            trace = why_runtime.read_trace(args.trace)
        if args.timeline:
            timeline = _load_json(args.timeline)
        if args.live_state:
            live_state = _load_json(args.live_state)
        if args.against:
            prev_audit = _load_json(args.against)
    except (OSError, ValueError) as error:
        print(f"error: cannot read dash input: {error}", file=sys.stderr)
        return 1
    if args.policy:
        from .policy import load_policy, PolicyError  # noqa: PLC0415
        try:
            policy = load_policy(args.policy)
        except (OSError, PolicyError) as error:
            print(f"error: cannot read policy {args.policy}: {error}",
                  file=sys.stderr)
            return 1

    board = dash.Dashboard(
        ir, live_state=live_state, trace=trace, timeline=timeline,
        prev_audit=prev_audit, accepted=set(args.accept),
        accept_all=args.accept_all, policy=policy, mcp_scope=args.mcp_scope)

    if args.json:
        print(json.dumps(board.snapshot(), indent=2))
        return 0

    color = (not args.no_color) and sys.stdout.isatty()
    if args.watch:
        import time  # noqa: PLC0415
        try:
            while True:
                sys.stdout.write("\033[2J\033[H" if color else "\n")
                print(board.render(color=color), flush=True)
                time.sleep(max(0.1, args.interval))
        except KeyboardInterrupt:
            return 0
    print(board.render(color=color))
    return 0


def _run_repair(args) -> int:
    """`revl repair <files> --component NAME ...` — the repair loop (item 62).

    Boots the composition into a session, then runs the unattended loop:
    regenerate/reuse -> gauntlet -> policy -> widening-ack -> hot-swap, bounded
    by a self-repair policy, and prints the incident dossier. Exit status: 0 when
    the fault was repaired (or planned clean with --plan), 2 when the loop paused
    for a human ack (a widening), 1 otherwise (ineligible / rejected / no
    candidate)."""
    # the session boots a real cordis runtime; put backends/python on the path
    # exactly as `run` does.
    backend_dir = Path(__file__).resolve().parents[2] / "backends" / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    from .mcp import repair as _repair  # noqa: PLC0415
    from .mcp.session import Session, SessionError  # noqa: PLC0415

    try:
        ir = compile_files(args.files)
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if ir.get("holes"):
        print("error: the composition has open typed holes; fill them before "
              "repair", file=sys.stderr)
        return 1

    session = Session()
    try:
        session.load(ir, record=not args.no_record)
    except SessionError as error:
        print(f"error: cannot boot the composition: {error}", file=sys.stderr)
        return 1

    if args.boundary_policy:
        from .policy import load_policy, PolicyError  # noqa: PLC0415
        try:
            session.boundary_policy = load_policy(args.boundary_policy)
        except (OSError, PolicyError) as error:
            print(f"error: cannot read boundary policy: {error}", file=sys.stderr)
            return 1

    arguments: dict = {"component": args.component, "apply": not args.plan,
                       "accept": list(args.accept)}
    if args.trace:
        try:
            arguments["traceFile"] = args.trace
        except OSError as error:  # pragma: no cover
            print(f"error: cannot read trace: {error}", file=sys.stderr)
            return 1
    if args.predicate:
        arguments["predicate"] = args.predicate
    if args.candidate:
        try:
            arguments["candidate"] = {
                "source": "\n".join(Path(p).read_text(encoding="utf-8")
                                    for p in args.candidate)}
        except OSError as error:
            print(f"error: cannot read candidate: {error}", file=sys.stderr)
            return 1
    if args.self_repair_policy:
        try:
            arguments["selfRepairPolicy"] = Path(
                args.self_repair_policy).read_text(encoding="utf-8")
        except OSError as error:
            print(f"error: cannot read self-repair policy: {error}",
                  file=sys.stderr)
            return 1

    dossier = _repair.run_repair(session, arguments)

    if args.json:
        print(json.dumps(dossier, indent=2))
    else:
        print(_repair.render_incident(dossier))

    status = (dossier.get("incident") or {}).get("status")
    if status in (_repair.STATUS_REPAIRED, _repair.STATUS_PLANNED):
        return 0
    if status == _repair.STATUS_AWAITING_ACK:
        return 2
    return 1


def _run_recover(args) -> int:
    """`revl recover --wal FILE` — crash recovery over a write-ahead log
    (docs/crash-recovery.md). Reads the WAL, decides roll-forward vs roll-back,
    runs the reconstructible boundary inverses (roll-back) or resumes the
    persisted generation (roll-forward), and prints a checked verdict with a
    residue proof. Exit status follows the residue: 0 when clean, 1 when honest
    residue remains."""
    # the recovery module reads `replay.WriteAheadLog`, a backend module — put
    # backends/python on the path exactly as `run` does, but *without* needing a
    # cordis runtime (recovery works from the durable log, the process is dead).
    backend_dir = Path(__file__).resolve().parents[2] / "backends" / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    from .recovery import recover, render, RecoveryError  # noqa: PLC0415

    session = snapshot = None
    if getattr(args, "restore", None):
        try:
            with open(args.restore, encoding="utf-8") as handle:
                snapshot = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            print(f"error: cannot read snapshot {args.restore}: {error}",
                  file=sys.stderr)
            return 1
        from .mcp.session import Session  # noqa: PLC0415 — lazy: cordis only if resuming
        session = Session()

    try:
        report = recover(args.wal, session=session, snapshot=snapshot)
    except RecoveryError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))
    return 0 if report.get("residue", {}).get("clean") else 1


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
    audit.add_argument(
        "--policy", metavar="POLICY", default=None,
        help="boundary-policy gate (item 33): evaluate a policy file over the "
             "audit graph and REFUSE admission (nonzero) if any component "
             "reaches a capability it may not (allow/deny per component or "
             "realm, `tenants never reach each other`, the mcp/agent sandbox)")
    audit.add_argument(
        "--mcp-scope", action="append", default=[], metavar="COMPONENT",
        help="treat COMPONENT as MCP/agent-admitted so the policy's `mcp` "
             "sandbox allow-list applies to it (repeatable); `*` = every "
             "component")

    version_cmd = sub.add_parser(
        "version",
        help="derive the required semver bump from the interface diff against "
             "a previous composition (docs/derived-versioning.md)")
    version_cmd.add_argument("files", nargs="+")
    version_cmd.add_argument(
        "--against", metavar="PREV.json", default=None,
        help="a previous compiled composition document to diff against; the "
             "bump is a measurement of the change (produce one with `revl "
             "compile <sources> -o prev.json` or `--emit-manifest`)")
    version_cmd.add_argument(
        "--current-version", metavar="X.Y.Z", default=None,
        help="the previous composition's declared version; when given, the "
             "computed next version is printed too")
    version_cmd.add_argument(
        "--emit-manifest", action="store_true",
        help="print the compiled composition document (the diff input a later "
             "`--against` reads) and exit, instead of deriving a bump")
    version_cmd.add_argument("--json", action="store_true",
                             help="machine-readable derivation")

    contract = sub.add_parser(
        "contract",
        help="federated contracts between sovereign compositions: export a "
             "consumer surface, or check a provider against a pinned one "
             "(docs/federation.md)")
    contract_sub = contract.add_subparsers(dest="contract_command", required=True)
    contract_export = contract_sub.add_parser(
        "export",
        help="project composition A's compiled IR into its consumer surface — "
             "the pinnable contract of everything A requires from a provider")
    contract_export.add_argument("files", nargs="+")
    contract_export.add_argument(
        "--consumer", metavar="LABEL", default=None,
        help="a name for the consumer, echoed into the artifact and its "
             "verdicts (defaults to none)")
    contract_check = contract_sub.add_parser(
        "check",
        help="does a provider's current manifest still satisfy a consumer's "
             "pinned surface? FAILs (nonzero) on a §5 drift that breaks it")
    contract_check.add_argument(
        "--consumer", metavar="A-pinned.json", required=True,
        help="the consumer surface a provider must satisfy (produce it with "
             "`revl contract export <A-sources>`)")
    contract_check.add_argument(
        "--provider", metavar="B", required=True, nargs="+",
        help="the provider's current composition: its .rvl sources (compiled "
             "here), or a single compiled manifest .json (`revl compile -o` / "
             "`revl version --emit-manifest`)")
    contract_check.add_argument("--json", action="store_true",
                                help="machine-readable verdict")

    erase = sub.add_parser(
        "erase-report",
        help="right-to-erasure evidence for one realm: in-process state gone "
             "(no-residue proof), boundary crossings compensated-vs-bare, and "
             "other realms provably untouched (docs/erase-report.md)")
    erase.add_argument("files", nargs="+")
    erase.add_argument("--realm", required=True, metavar="R",
                       help="the realm to report erasure evidence for")
    erase.add_argument("--json", action="store_true",
                       help="machine-readable, versioned report document")
    erase.add_argument("--no-residue-proof", action="store_true",
                       help="skip the runtime teardown proof (static sections "
                            "only; use where the cordis runtime is unavailable)")

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

    undo_cmd = sub.add_parser(
        "undo", help="operator undo: replay a generation history and return to an "
                     "earlier generation THROUGH THE GATE (docs/generation-history.md)")
    undo_cmd.add_argument("history", metavar="history.json",
                          help="a revl.generation-history document (the session's "
                               "history export): the retained generation snapshots")
    undo_cmd.add_argument("--to", type=int, default=None, metavar="GEN",
                          help="a recorded generation number to return to; omit to "
                               "undo to the immediately previous generation (N−1)")
    undo_cmd.add_argument("--json", action="store_true", help="machine-readable output")


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

    # historical mode (docs/queries.md §9): the same envelope, over a RECORDED
    # run instead of a static IR. These read files, not source, so they sit
    # outside the compile-from-source loop above. (Live mode is session-bound —
    # it has no one-shot CLI entry; use the MCP `revl_live_query` tool.)
    between = query_sub.add_parser(
        "emitted-between",
        help="which emissions crossed between steps X and Y (a recorded replay "
             "timeline JSON)?")
    between.add_argument("--timeline", required=True, metavar="FILE",
                         help="a replay recording JSON (a `revl_timeline` dump)")
    between.add_argument("--from", dest="frm", type=int, required=True,
                         metavar="X", help="first step index (inclusive)")
    between.add_argument("--to", type=int, required=True, metavar="Y",
                         help="last step index (inclusive)")
    between.add_argument("--component", default=None,
                         help="restrict to one component; omit for all")
    between.add_argument("--json", action="store_true",
                         help="machine-readable output")

    touched = query_sub.add_parser(
        "touched",
        help="everything a component touched during its life (item-27 lifecycle "
             "trace + optional replay recording)")
    touched.add_argument("component", metavar="COMPONENT")
    touched.add_argument("--trace", default=None, metavar="FILE",
                         help="an item-27 lifecycle JSONL (`revl run --trace`) "
                              "for the load/withdraw span")
    touched.add_argument("--timeline", default=None, metavar="FILE",
                         help="a replay recording JSON for the effects/emissions")
    touched.add_argument("--json", action="store_true",
                         help="machine-readable output")

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
    test.add_argument("--mock-requires", action="store_true",
                      help="run every `lifecycle test` in mock world: each unmet "
                           "`requires` is filled by an auto-generated mock provider "
                           "(item-37-typed, seeded; emissions recorded-not-crossed), "
                           "so a consumer boots with zero real providers "
                           "(py tier; docs/auto-mocks.md)")

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
    # operator capabilities (docs/operator-capabilities.md, item 55): scope the
    # session's management verbs to one operator's grants. Opt-in — omit for
    # today's ungated behaviour.
    mcp_serve.add_argument("--operator-profile", default=None, metavar="PROFILE",
                           help="bound the management verbs this session may call "
                                "(swap/unload/restore/undo/edit/load/snapshot) to "
                                "an operator's declared grants (item 55); a DSL or "
                                "JSON file. Omit for ungated (root over transport)")
    mcp_serve.add_argument("--operator", default=None, metavar="TOKEN",
                           help="which operator in the profile this session runs "
                                "as (its session token); optional when the profile "
                                "declares exactly one operator")
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

    # `revl export wit` — the reverse of `revl import wit` (docs/wit-bridge.md).
    # Additive: its own `export` group, mirroring the `import` family's shape.
    exp_cmd = sub.add_parser(
        "export",
        help="export a revl service/composition as an external interface "
             "definition (the reverse of `revl import`)")
    exp_sub = exp_cmd.add_subparsers(dest="export_command", required=True)
    exp_wit = exp_sub.add_parser(
        "wit",
        help="generate the standard WIT interface for a revl service or "
             "composition (docs/wit-bridge.md)")
    exp_wit.add_argument("files", nargs="+", help=".rvl source files")
    exp_group = exp_wit.add_mutually_exclusive_group(required=True)
    exp_group.add_argument("--service", default=None, metavar="NAME",
                           help="export a single service by name")
    exp_group.add_argument("--composition", action="store_true",
                           help="export every service the composition provides")
    exp_wit.add_argument("--package", default="revl:exported", metavar="NS:NAME",
                         help="WIT package id for the generated file "
                              "(default: revl:exported)")
    exp_wit.add_argument("-o", "--output", default=None,
                         help="output path (default: stdout)")
    exp_wit.add_argument("--json-diagnostics", action="store_true",
                         help="on rejection, print a structured diagnostic instead "
                              "of the human rendering")

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
                     help="target runtime tier (default: py; py, rust, java and "
                          "wasm are runnable — rust/java/wasm boot as a "
                          "separate process, --once for the "
                          "boot/teardown round-trip; a missing runtime is a skip with a "
                          "reason and a nonzero exit; ts and go emit but have no "
                          "run driver yet)")
    run.add_argument("--config", default=None,
                     help="TOML/JSON file of `component-name = { ... }` config tables")
    run.add_argument("--watch", action="store_true",
                     help="watch the sources and recompile on change; a rejected edit is refused, the run keeps going")
    run.add_argument("--record", action="store_true",
                     help="record the effect accumulator so the REPL can step "
                          "backwards over it (`:timeline`, `:back k`) — see docs/replay.md")
    run.add_argument("--wal", default=None, metavar="FILE",
                     help="persist the effect accumulator as a durable write-ahead "
                          "log (implies --record). On restart, `revl recover --wal "
                          "FILE` rolls forward or back and states a checked verdict "
                          "(docs/crash-recovery.md)")
    run.add_argument("--trace", default=None, metavar="FILE",
                     help="write a causal lifecycle trace (JSONL) — every "
                          "transition carries the cause chain behind it, "
                          "queryable with `revl why <c> --trace FILE` "
                          "(docs/why-runtime.md)")
    run.add_argument("--withdraw", default=None, metavar="COMPONENT",
                     help="one-shot: boot, withdraw this live component while "
                          "recording the causal cascade, then diff the actual "
                          "cascade against the static `withdraw` prediction "
                          "(the runtime oracle) and tear down")
    run.add_argument("--plan", action="store_true",
                     help="print the load plan (order, config, callable keys) and exit, without a runtime")
    run.add_argument("--placement", default=None,
                     help="TOML/JSON placement map: split components across processes and wire the seams")
    run.add_argument("--once", action="store_true",
                     help="bring the composition up, then tear down LIFO and exit "
                          "(with --placement: run probes across processes first; "
                          "with --backend rust/java/wasm: boot the tier's process "
                          "(cordis-rs / cordis4j on a JVM / cordis-wasm on wasmtime), "
                          "prove no residue, exit)")

    recover = sub.add_parser(
        "recover",
        help="crash recovery: read a `revl run --wal` write-ahead log and roll "
             "forward (resume the persisted generation) or roll back (run the "
             "boundary inverses LIFO), ending in a checked verdict + residue "
             "proof (docs/crash-recovery.md)")
    recover.add_argument("--wal", required=True, metavar="FILE",
                         help="a write-ahead log written by `revl run --wal`")
    recover.add_argument("--restore", default=None, metavar="SNAPSHOT.json",
                         help="on roll-forward, the item-15 snapshot to re-admit "
                              "so recovery resumes the persisted generation")
    recover.add_argument("--json", action="store_true", help="machine-readable output")

    why = sub.add_parser(
        "why",
        help="explain a recorded lifecycle transition — the cause chain for a "
             "component in a `revl run --trace` JSONL trace (docs/why-runtime.md)")
    why.add_argument("component", help="the component whose transition to explain")
    why.add_argument("--trace", required=True, metavar="FILE",
                     help="a JSONL causal trace written by `revl run --trace`")
    why.add_argument("--check", nargs="+", default=None, metavar="FILE",
                     help="also run the oracle: compile these source files and "
                          "diff the static `withdraw` prediction against the "
                          "recorded cascade; a mismatch is a defect (nonzero exit)")
    why.add_argument("--json", action="store_true", help="machine-readable output")

    dash = sub.add_parser(
        "dash",
        help="the supervisor's cockpit (item 63): a READ-ONLY live view over a "
             "session or a recorded run — the dependency graph (realms, seams), "
             "the causal trace streaming, and the pending-decisions queue "
             "(boundary-widening acks, policy exceptions) with evidence "
             "attached (docs/dash.md)")
    dash.add_argument("files", nargs="+",
                      help=".rvl sources — the composition whose graph to show")
    dash.add_argument("--trace", default=None, metavar="FILE",
                      help="an item-27 lifecycle JSONL (`revl run --trace`): "
                           "streams the causal pane with no live runtime")
    dash.add_argument("--timeline", default=None, metavar="FILE",
                      help="a replay recording JSON (a `revl_timeline` dump) for "
                           "the effect/emission detail behind the lifecycle")
    dash.add_argument("--live-state", default=None, metavar="FILE",
                      help="a live-state snapshot JSON "
                           "({generation, servedKeys, componentStates}, from a "
                           "running session): colors the graph as it stands now")
    dash.add_argument("--against", default=None, metavar="PREV.json",
                      help="a previous `audit --json` document; the boundary "
                           "additions since it become the widening queue (item 21)")
    dash.add_argument("--accept", action="append", default=[], metavar="CROSSING",
                      help="mark one added crossing as already acknowledged in "
                           "the queue (the token printed after `+`; repeatable)")
    dash.add_argument("--accept-all", action="store_true",
                      help="mark every added crossing as acknowledged")
    dash.add_argument("--policy", default=None, metavar="POLICY",
                      help="a boundary policy file (item 33); its violations over "
                           "the current audit are the policy-exception queue, "
                           "each with its why-trace as evidence")
    dash.add_argument("--mcp-scope", action="append", default=[], metavar="COMPONENT",
                      help="treat COMPONENT as MCP/agent-admitted for the policy's "
                           "`mcp` sandbox (repeatable); `*` = every component")
    dash.add_argument("--watch", action="store_true",
                      help="periodic-refresh loop: re-read the sources and reprint "
                           "on an interval (read-only; Ctrl-C to stop)")
    dash.add_argument("--interval", type=float, default=2.0, metavar="SECONDS",
                      help="refresh interval for --watch (default: 2.0)")
    dash.add_argument("--no-color", action="store_true",
                      help="plain output with no ANSI color")
    dash.add_argument("--json", action="store_true",
                      help="print the structured model instead of the text view")

    repair = sub.add_parser(
        "repair",
        help="the repair loop (item 62): a faulting component fixes itself, "
             "within policy — regenerate/reuse -> gauntlet -> policy -> "
             "widening-ack -> hot-swap, unattended, with an incident dossier "
             "(docs/repair-loop.md)")
    repair.add_argument("files", nargs="+",
                        help=".rvl sources — the running composition to repair")
    repair.add_argument("--component", required=True,
                        help="the faulting component to repair")
    repair.add_argument("--trace", default=None, metavar="FILE",
                        help="a JSONL causal trace (`revl run --trace`): the "
                             "fault's why (item 27)")
    repair.add_argument("--candidate", action="append", default=[], metavar="FILE",
                        help="the regenerated repair source(s) — a whole "
                             "composition to swap in (repeatable)")
    repair.add_argument("--self-repair-policy", default=None, metavar="FILE",
                        help="which components may self-repair and which "
                             "capabilities a repair may touch; absent = closed "
                             "(nothing self-repairs)")
    repair.add_argument("--boundary-policy", default=None, metavar="FILE",
                        help="an item-33 boundary policy for the reach gate")
    repair.add_argument("--predicate", default=None, metavar="EXPR",
                        help="a bisect predicate to slice the fault to a step "
                             "(item 40)")
    repair.add_argument("--accept", action="append", default=[], metavar="CROSSING",
                        help="acknowledge a widening crossing (item 21 ack "
                             "token; repeatable)")
    repair.add_argument("--plan", action="store_true",
                        help="run every gate but do not swap (a rehearsal)")
    repair.add_argument("--no-record", action="store_true",
                        help="load without recording (disables the timeline "
                             "slice; the loop still runs)")
    repair.add_argument("--json", action="store_true",
                        help="print the incident dossier as JSON")

    args = parser.parse_args(argv)

    if args.command == "explain":
        return _run_explain(args)

    if args.command == "repair":
        return _run_repair(args)

    if args.command == "fmt":
        return _run_fmt(args)

    if args.command == "run":
        return run_command(args)

    if args.command == "why":
        return _run_why(args)

    if args.command == "dash":
        return _run_dash(args)

    if args.command == "recover":
        return _run_recover(args)

    if args.command == "serve":
        return _run_serve(args)

    if args.command == "mcp":
        return _run_mcp(args)

    if args.command == "import":
        return _run_import(args)

    if args.command == "export":
        return _run_export(args)

    if args.command == "plan":
        return _run_plan(args)

    if args.command == "apply":
        return _run_apply(args)
    if args.command == "undo":
        return _run_undo(args)

    if args.command == "contract":
        return _run_contract(args)

    # historical query mode reads a recorded run (files, not source), so it is
    # routed before the compile-from-source step every other command shares
    if args.command == "query" and args.query_command in ("emitted-between",
                                                           "touched"):
        return _run_history_query(args)

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
        return test_command(ir, args.backend, sweep=getattr(args, "sweep", False),
                            mock_requires=getattr(args, "mock_requires", False))

    if args.command == "query":
        return _run_query(args, ir)

    if args.command == "erase-report":
        from .erase_report import build_report, render  # noqa: PLC0415
        report_doc = build_report(
            ir, args.realm,
            prove_residue=not getattr(args, "no_residue_proof", False))
        if args.json:
            print(json.dumps(report_doc, indent=2))
        else:
            print(render(report_doc))
        if not report_doc.get("ok"):
            return 1
        # a proven state-gone + untouched other realms is a clean report;
        # a bare crossing does not fail (it is enumerated, by design), but an
        # unproven teardown or a breached other realm does.
        state = report_doc["inProcessStateGone"]["noResidueProof"]
        residue_bad = state.get("available") and not state.get("proven")
        breached = not report_doc["otherRealmsUntouched"]["untouched"]
        return 1 if (residue_bad or breached) else 0

    if args.command == "audit" and getattr(args, "policy", None):
        # the third leg of the gate (item 33): absolute authority. The policy
        # is evaluated as set operations over the same audit graph `--diff`
        # and `--json` already build; a violation refuses admission with a
        # why-trace naming the offending chain.
        from .audit_diff import audit_report  # noqa: PLC0415
        from .policy import evaluate, load_policy, render_report  # noqa: PLC0415

        policy = load_policy(args.policy)
        audit = audit_report(ir)
        scope = args.mcp_scope
        mcp_components = (frozenset(audit.get("boundary") or {})
                         if "*" in scope else frozenset(scope))
        violations = evaluate(policy, audit, mcp_components=mcp_components)
        if args.json:
            print(json.dumps(
                {"policy": args.policy,
                 "violations": [{"kind": v.kind, "component": v.component,
                                 "token": v.token, "message": v.message,
                                 "why": v.why.to_json()} for v in violations],
                 "refused": bool(violations)}, indent=2))
        else:
            print(render_report(policy, violations))
        return 1 if violations else 0

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
        instances = manifest.get("instances") or []
        if instances:
            # Capability attenuation per instance (item 66,
            # docs/capability-attenuation.md): the spawner → child narrowing.
            # A child's granted set is a checked subset of what the spawner
            # holds; `attenuated` is the authority dropped on the way down —
            # the least-authority proof, per lineage edge.
            print("\ncapability attenuation (per instance — lineage narrows, "
                  "never widens):")
            for edge in instances:
                holds = ", ".join(edge.get("holds") or []) or "—"
                granted = ", ".join(edge.get("granted") or []) or "—"
                dropped = ", ".join(edge.get("attenuated") or [])
                tail = f"  (dropped: {dropped})" if dropped else ""
                print(f"  {edge['parent']} → {edge['child']}: "
                      f"holds [{holds}] ⊇ grants [{granted}]{tail}")
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

    if args.command == "version":
        from .version import derive, render  # noqa: PLC0415
        if args.emit_manifest:
            # the diff input for a later `--against`: the compiled composition,
            # which (unlike an audit report) carries the `services` table.
            print(json.dumps(ir, indent=2))
            return 0
        if not args.against:
            print("error: `revl version` needs --against PREV.json (a previous "
                  "compiled composition) or --emit-manifest", file=sys.stderr)
            return 2
        try:
            with open(args.against, encoding="utf-8") as handle:
                previous = json.load(handle)
        except OSError as error:
            print(f"error: cannot read {args.against}: {error}", file=sys.stderr)
            return 1
        try:
            result = derive(previous, ir, previous_version=args.current_version)
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(render(result, args.against))
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

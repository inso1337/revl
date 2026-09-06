"""Per-command CLI handlers: fmt and the bridge family (mcp / serve / import / export / contract).

Pure move — per-command CLI handlers, byte-identical behavior; see revl.__main__ for dispatch.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..compiler import compile_files
from ..diagnostics import report
from ..errors import RevlError
from ..fmt import migrate_source


def _run_fmt(args: argparse.Namespace) -> int:
    """`revl fmt`: canonical formatter with a self-proving IR-equivalence gate.

    Default mode produces a canonical formatting; `--migrate` rewrites 1.x
    `$` interpolation to 2.0 templates.  Either way the rewrite is admitted
    only when compiling the original and the rewritten text yields
    byte-identical IR (roadmap item 35); a file whose IR would change is
    REFUSED (named, nonzero exit) rather than written.
    """
    from ..formatter import format_source, ir_equivalent, FormatError

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
        if gate.warning:
            # a check the gate could not run to completion: never silent.
            print(f"warning: {gate.warning}", file=sys.stderr)

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
    from ..mcp.schema import import_tools, tools_from_ir
    from ..mcp.server import serve

    if args.mcp_command == "serve":
        # authoring trust: the operator's answer to "may the agent driving this
        # server author host code, and what filesystem may it name?". Default
        # closed; the server refuses an agent-authored `extern`/host block and
        # confines every path argument to the sanctioned roots.
        from ..mcp.server import set_authoring_trust

        providers: dict = {}
        for path in getattr(args, "provider", None) or []:
            try:
                with open(path, encoding="utf-8") as handle:
                    providers[path] = handle.read()
            except OSError as error:
                print(f"error: cannot read provider module {path}: {error}",
                      file=sys.stderr)
                return 1
        grants = getattr(args, "grant", None) or []
        set_authoring_trust(
            host_code=getattr(args, "author_trust", "untrusted") == "trusted",
            granted=frozenset(grants) if grants else None,
            providers=providers or None,
            roots=tuple(getattr(args, "root", None) or ()) or None,
        )
        # operator capabilities (docs/operator-capabilities.md, item 55): bind
        # the served session to one operator identity, so its management verbs
        # are scoped by that operator's grants. No profile => ungated (today's
        # root-over-transport), so this is opt-in for networked/multi-operator
        # use.
        if getattr(args, "operator_profile", None):
            from ..mcp.operator import ProfileError, load_profile
            from ..mcp.server import SESSION

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
        # boundary policy (item 33): bind a policy to the session so its agent
        # sandbox is enforced and, with `leases enforced`, the item-61 lease
        # advisory becomes an admission refusal. Opt-in, like the profile above.
        if getattr(args, "policy", None):
            from ..policy import PolicyError, load_policy
            from ..mcp.server import SESSION

            try:
                SESSION.sandbox = load_policy(args.policy)
            except (OSError, PolicyError) as error:
                print(f"error: cannot load policy {args.policy}: {error}",
                      file=sys.stderr)
                return 1
        # auto-approve policy (item 246): the second orthogonal gate, off unless
        # named here. Enabling it REQUIRES recording (enforced at load). With no
        # operator profile that WITHHOLDS `approve` from the calling identity, the
        # class-(c) prompt is self-answerable and the gate is advisory — warn at
        # startup naming the hole (Decision 4; the diagnostic is not optional).
        if getattr(args, "approval_policy", None):
            from ..mcp.server import SESSION

            SESSION.approval_policy = args.approval_policy
            operator = getattr(SESSION, "operator", None)
            self_approvable = operator is None or any(
                g.allow and g.covers_verb("approve")
                for g in getattr(operator, "grants", ()))
            if self_approvable:
                print("warning: the approval policy is enabled but the calling "
                      "identity can answer its own class-(c) tickets (no operator "
                      "profile withholds `approve`), so the per-call prompt is "
                      "advisory, not a gate — bind --operator-profile that grants "
                      "`approve` only to the human's identity (item 246, "
                      "Decision 4)", file=sys.stderr)
        # roadmap 425 F3 / 427 F5: the durability posture for an approved
        # crossing's caller-supplied resource value. Read unconditionally (it has
        # a default), so it applies whether or not the approval policy is on.
        values = getattr(args, "approval_record_values", None)
        if values:
            from ..mcp.server import SESSION

            SESSION.approval_record_values = values
        # composition persistence (docs/persistence.md): a snapshot passed on
        # the command line is re-admitted through the same gate a live restore
        # runs — a component the current checker rejects aborts the boot loudly
        # rather than being smuggled in.
        if getattr(args, "restore", None):
            from ..mcp.persist import RestoreError
            from ..mcp.server import SESSION

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
    from ..run import (  # noqa: PLC0415
        _env_contract_problem, _load_config, _load_env, _merge_env,
        _required_config_problem,
    )

    if not getattr(args, "mcp", False):
        print("error: `revl serve` needs a transport — pass --mcp to serve over "
              "the MCP stdio protocol", file=sys.stderr)
        return 2

    from ..holes import refuse_admission  # noqa: PLC0415

    try:
        ir = compile_files(args.files)
        # booting is admission: a draft with open obligations may not become a
        # running composition, however it was compiled (docs/holes.md)
        refuse_admission(ir)
        config = _load_config(getattr(args, "config", None))
        env = _load_env(getattr(args, "env", None))
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"error: cannot read config: {error}", file=sys.stderr)
        return 1

    # item 350: the environment contract, checked with the same rules `revl run`
    # applies — serving a composition is booting it, so the boot component's
    # `--env` door and its declared bounds hold here too.
    problem = _env_contract_problem(ir, env, config)
    if problem is not None:
        print(f"error: {problem}", file=sys.stderr)
        return 1
    config = _merge_env(ir, env, config)

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

    from ..mcp.composed import serve_composition  # noqa: PLC0415
    from ..mcp.session import SessionError  # noqa: PLC0415

    try:
        return serve_composition(ir, config, composition=args.composition)
    except SessionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 3


def _run_import(args) -> int:
    """`revl import {wit,openapi,cordis,a2a}` — the import codegen family
    (docs/import-wit.md, docs/import-openapi.md, docs/import-cordis.md,
    docs/import-a2a.md)."""
    try:
        if args.import_command == "openapi":
            from ..import_openapi import import_openapi_file
            source = import_openapi_file(args.file, backend=args.backend,
                                         service=args.service, pure=args.pure,
                                         emission=args.emission,
                                         compensate=args.compensate,
                                         preimage=args.preimage, undo=args.undo,
                                         if_match=args.if_match,
                                         undo_key=args.undo_key,
                                         require_if_match=args.require_if_match)
        elif args.import_command == "cordis":
            from ..import_cordis import import_cordis_file
            source = import_cordis_file(args.file, backend=args.backend,
                                        service=args.service, pure=args.pure,
                                        mark_unrecovered=args.mark_unrecovered)
        elif args.import_command == "a2a":
            from ..import_a2a import import_a2a_file
            source = import_a2a_file(args.file, backend=args.backend,
                                     service=args.service,
                                     allow_plaintext=args.allow_plaintext,
                                     follow_redirects=args.follow_redirects)
        else:
            from ..import_wit import import_wit_file
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
    """`revl export {wit,client}` — project a compiled IR into an external face.

    `wit` (docs/wit-bridge.md) is the reverse of `revl import wit`: pure IR
    codegen of the standard WIT interface a revl service or composition presents
    (the importer's type mapping, run backwards). No runtime, no emission, no
    binary — interface text only; effects ride alongside the shape as
    `/// @revl:*` doc comments, because WIT's type system carries shape, not
    lifecycle.

    `client` (docs/interop-bridge.md, item 424 gap (c) slice C1) is a typed
    remote client for a NON-revl consumer, over the canonical value encoding the
    bridges already speak. Also pure codegen: the client is typed and bounded
    LOCALLY and makes no claim about the callee (D-424c.8).
    """
    try:
        ir = compile_files(args.files)
    except RevlError as error:
        if getattr(args, "json_diagnostics", False):
            print(json.dumps(report(error), indent=2))
        else:
            print(f"error: {error}", file=sys.stderr)
        return 1

    if args.export_command == "client":
        from ..export_client import export_client  # noqa: PLC0415
        try:
            source = export_client(ir, lang=args.lang, service=args.service,
                                   composition=args.composition)
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

    from ..export_wit import export_wit  # noqa: PLC0415

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


def _run_contract(args) -> int:
    """`revl contract` — federated contracts between sovereign compositions
    (docs/federation.md, roadmap item 58).

    `export` projects composition A's compiled IR into its consumer surface
    (the pinnable contract of what A requires from a provider). `check` runs a
    provider B's current manifest against a pinned surface through the same
    §5/drift predicate `revl version` uses (`version.diff_services`): a MAJOR
    drift is a contract break, and the gate exits nonzero naming it.
    """
    from ..federation import check, consumer_surface, render

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

"""Per-command CLI handlers: fmt, sourcemap and the bridge family (mcp / serve / import / export / contract).

Pure move — per-command CLI handlers, byte-identical behavior; see revl.__main__ for dispatch.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from contextlib import contextmanager
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


def _bind_session_authority(args) -> int | None:
    """Bind the operator profile, boundary policy and approval policy named
    on the command line to the compiler server's session. Shared by
    `revl mcp serve` and `revl mcp proxy`, so a proxied session answers to the
    same authority a served one does. Returns an exit code on a refusal."""
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
        if getattr(args, "http", None):
            # issue #1463: over HTTP every REQUEST is bound to its own operator
            # (revl.mcp.http_transport); the process runs as nobody, so there is
            # no identity for an unauthenticated request to fall back to
            SESSION.operator_registry = registry
        else:
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
            # item 471 / issue #979: the session runs AS one operator, but a
            # multi-party question is answered by several. The whole registry is
            # what a cast attributed to another operator is checked against
            # (`revl.mcp.quorum.resolve_cast`); without it, a second identity
            # cannot be proven and every such cast is refused.
            SESSION.operator_registry = registry
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
        if getattr(args, "http", None):
            # over HTTP each caller is its own operator: the hole is an operator
            # that may both make a gated call and approve it
            both = sorted(
                o.token for o in SESSION.operator_registry.operators.values()
                if any(g.allow and g.covers_verb("call") for g in o.grants)
                and any(g.allow and g.covers_verb("approve") for g in o.grants))
            self_approvable = False
            if both:
                print(f"warning: operator(s) {', '.join(both)} may both call and "
                      f"approve, so each can answer its own class-(c) tickets; "
                      f"grant `approve` only to the human's identity (item 246, "
                      f"Decision 4)", file=sys.stderr)
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
    return None


def _http_exposure(args):
    """`(Exposure, None)` for `--http HOST:PORT`, or `(None, exit code)` after
    saying why HTTP mode cannot start (issue #1463)."""
    from ..mcp.http_guard import Exposure

    host, sep, port = (args.http or "").rpartition(":")
    if not sep or not host or not port.isdigit():
        print(f"error: --http expects HOST:PORT, got {args.http!r}", file=sys.stderr)
        return None, 1
    if not getattr(args, "operator_profile", None):
        print("error: --http needs --operator-profile: every HTTP request is "
              "bound to one of its operators, and a request with no identity is "
              "refused, never run as a default operator", file=sys.stderr)
        return None, 1
    if getattr(args, "operator", None):
        print("error: --operator does not apply with --http: each request "
              "authenticates as its own operator, and the process runs as none",
              file=sys.stderr)
        return None, 1
    exposure = Exposure(host=host.strip("[]"), port=int(port),
                        tls_cert=args.tls_cert, tls_key=args.tls_key,
                        tls_client_ca=args.tls_client_ca,
                        allow_hosts=tuple(args.allow_host or ()),
                        allow_origins=tuple(args.allow_origin or ()))
    return exposure, None


def _stdio_live_profile(args):
    """`(hook, None)` that re-binds the stdio session to the operator profile
    file before each message, `(None, None)` with no profile, or `(None, exit
    code)` when the file cannot be loaded (issue #1463, `live_profile`)."""
    if not getattr(args, "operator_profile", None):
        return None, None
    from ..mcp import server as _server
    from ..mcp.live_profile import ProfileSource, ProfileUnavailable, StdioBinding

    try:
        source = ProfileSource(args.operator_profile,
                               settle_ms=getattr(args, "profile_settle_ms", 1000))
    except (ProfileUnavailable, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return None, 1
    return StdioBinding(source, _server, _server.SESSION.operator.token), None


def _run_mcp(args) -> int:
    """`revl mcp {serve,schema,import,proxy}`: the MCP bridge (docs/mcp-bridge.md)."""
    from ..mcp.schema import import_tools, tools_from_ir
    from ..mcp.server import serve

    if args.mcp_command == "serve":
        _announce_or_reexec_runtime(args)
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
        exposure = None
        if getattr(args, "http", None):
            exposure, code = _http_exposure(args)
            if exposure is None:
                return code
        refused = _bind_session_authority(args)
        if refused is not None:
            return refused
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
        if exposure is not None:
            from ..mcp import server as _server
            from ..mcp.http_transport import (HttpTransport, ServerDispatcher,
                                              TransportError)

            try:
                transport = HttpTransport(ServerDispatcher(_server),
                                          exposure=exposure, auth=args.auth,
                                          server_module=_server,
                                          profile_path=args.operator_profile,
                                          profile_settle_ms=args.profile_settle_ms)
            except TransportError as error:
                print(f"error: {error}", file=sys.stderr)
                return 1
            with _sigterm_unwinds():
                return transport.serve_forever()
        live, code = _stdio_live_profile(args)
        if code is not None:
            return code
        return serve(before=live)

    if args.mcp_command == "schema":
        try:
            ir = compile_files(args.files)
        except RevlError as error:
            print(json.dumps(report(error), indent=2))
            return 1
        print(json.dumps({"tools": tools_from_ir(ir, composition=args.composition)},
                         indent=2))
        return 0

    if args.mcp_command == "proxy":
        return _run_mcp_proxy(args)

    # import
    try:
        with open(args.manifest, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        print(f"error: cannot read {args.manifest}: {error}", file=sys.stderr)
        return 1
    from ..mcp.schema import parse_undo_specs
    try:
        source = import_tools(manifest, service=args.service, key=args.key,
                              backend=args.backend,
                              undo=parse_undo_specs(getattr(args, "undo", None)))
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(source)
    else:
        print(source, end="")
    return 0


def _run_mcp_proxy(args) -> int:
    """`revl mcp proxy [OPTIONS] -- COMMAND...` (issue #1463, docs/mcp-proxy.md)."""
    from ..mcp import proxy
    from ..mcp.schema import parse_undo_specs
    from ..mcp.server import SESSION

    command = list(args.upstream or [])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("error: name the upstream server to gate: "
              "`revl mcp proxy [OPTIONS] -- COMMAND [ARG ...]`", file=sys.stderr)
        return 1
    try:
        undo = parse_undo_specs(args.undo)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    http = None
    if args.http:
        exposure, code = _http_exposure(args)
        if exposure is None:
            return code
    # the proxy always runs the approval policy: gating is its whole purpose
    args.approval_policy = "auto"
    refused = _bind_session_authority(args)
    if refused is not None:
        return refused
    if args.http:
        http = {"exposure": exposure, "auth": args.auth,
                "profile_path": args.operator_profile,
                "profile_settle_ms": args.profile_settle_ms}
    if args.wal:
        SESSION._wal_path = args.wal
    live = None
    if http is None:
        live, code = _stdio_live_profile(args)
        if code is not None:
            return code
    if http is None:
        return proxy.run(command, undo=undo,
                         trust_read_only=args.trust_read_only_hints,
                         timeout=args.upstream_timeout, http=http, live=live)
    with _sigterm_unwinds():
        return proxy.run(command, undo=undo,
                         trust_read_only=args.trust_read_only_hints,
                         timeout=args.upstream_timeout, http=http, live=live)


def _run_serve(args) -> int:
    """`revl serve {--mcp,--http} FILES` — serve a composition's OWN provided
    operations (the fourth quadrant of the bridge, docs/mcp-bridge.md).

    Two transports over ONE projection. `--mcp` puts the provided operations on
    the MCP stdio wire; `--http` puts the SAME operations on HTTP, each as
    `POST /<composition>/<key>/<op>` over the canonical value encoding
    (docs/interop-bridge.md) — the server face `revl export client` pairs with
    (item 424 gap (c), D-424c.6). The transport decides nothing: the operation
    set, the checked emission hints and the wire shape are all the compiler's.

    Placement note: this boots one composition and stands it up, so it shares
    `revl run`'s admission-and-config preflight (compile -> refuse holes ->
    load config -> refuse a missing required field) rather than living under
    `revl mcp serve`, whose tool set is the fixed compiler surface.
    """
    from ..run import (  # noqa: PLC0415
        _env_contract_problem, _load_config, _load_env, _merge_env,
        _required_config_problem,
    )

    mcp = bool(getattr(args, "mcp", False))
    http = bool(getattr(args, "http", False))
    if not (mcp or http):
        print("error: `revl serve` needs a transport — pass --mcp to serve over "
              "the MCP stdio protocol, or --http to serve over HTTP",
              file=sys.stderr)
        return 2

    operator, code = _serve_operator_options(args, http)
    if code is not None:
        return code

    from ..holes import refuse_admission  # noqa: PLC0415
    from ..mcp.surface import declared_param_types  # noqa: PLC0415

    try:
        ir = compile_files(args.files)
        # booting is admission: a draft with open obligations may not become a
        # running composition, however it was compiled (docs/holes.md)
        refuse_admission(ir)
        # item 569 B1: the declared parameter types, read before the checker
        # strips `Trusted[...]`, so both faces withhold every operation that
        # takes an authority value.
        declared = declared_param_types(args.files)
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

    from ..mcp.session import SessionError  # noqa: PLC0415

    try:
        if http:
            from ..mcp.http_face import serve_http  # noqa: PLC0415
            from ..mcp.http_guard import Exposure, ExposureError  # noqa: PLC0415

            exposure = Exposure(host=args.host, port=args.port,
                                tls_cert=getattr(args, "tls_cert", None),
                                tls_key=getattr(args, "tls_key", None),
                                allow_hosts=tuple(getattr(args, "allow_host", None) or ()),
                                allow_origins=tuple(getattr(args, "allow_origin", None) or ()))
            from ..mcp.http_transport import TransportError  # noqa: PLC0415

            try:
                with _sigterm_unwinds():
                    return serve_http(ir, config, composition=args.composition,
                                      host=args.host, port=args.port, exposure=exposure,
                                      declared=declared,
                                      approval_policy=getattr(args, "approval_policy",
                                                              None),
                                      operator=operator,
                                      # the sources, so the operator listener can
                                      # snapshot and fork the served composition
                                      origin=({"files": [os.path.abspath(f)
                                                         for f in args.files]}
                                              if operator is not None else None),
                                      refuse_ungated_emissions=getattr(
                                          args, "refuse_ungated_emissions", False))
            except (ExposureError, TransportError) as error:
                print(f"error: {error}", file=sys.stderr)
                return 1
        from ..mcp.composed import serve_composition  # noqa: PLC0415
        return serve_composition(ir, config, composition=args.composition,
                                 declared=declared)
    except SessionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 3


@contextmanager
def _sigterm_unwinds():
    """Turn SIGTERM into the KeyboardInterrupt the HTTP serve loops already
    shut down cleanly on, for as long as they run (issue #1553).

    Without it a SIGTERM ended the process with no `finally`, so the E-Stop
    latch directory the MCP HTTP transport creates (`HaltLatch`) was left in
    the temp directory, by `revl mcp serve --http` and by `revl serve --http
    --operator-listen` alike. The previous handler is put back after; off the
    main thread, where no handler can be installed, nothing changes."""
    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGTERM, interrupt)
    except ValueError:
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _serve_operator_options(args, http: bool):
    """`(operator listener kwargs or None, None)` for `revl serve`, or `(None,
    exit code)` after saying why the options do not go together (issue #1553).

    The approval policy and the operator listener are `--http` options: `--mcp`
    serves one stdio client, which has no second listener to answer from."""
    listen = getattr(args, "operator_listen", None)
    profile = getattr(args, "operator_profile", None)
    if not http:
        for flag, value in (("--approval-policy", getattr(args, "approval_policy", None)),
                            ("--refuse-ungated-emissions",
                             getattr(args, "refuse_ungated_emissions", False)),
                            ("--operator-listen", listen),
                            ("--operator-profile", profile)):
            if value:
                print(f"error: {flag} applies to `revl serve --http` only",
                      file=sys.stderr)
                return None, 2
        return None, None
    if not listen:
        if profile:
            print("error: --operator-profile needs --operator-listen: the app "
                  "face authenticates nobody, and operators are served only on "
                  "the operator listener", file=sys.stderr)
            return None, 2
        return None, None
    host, sep, port = listen.rpartition(":")
    if not sep or not host or not port.isdigit():
        print(f"error: --operator-listen expects HOST:PORT, got {listen!r}",
              file=sys.stderr)
        return None, 2
    if not profile:
        print("error: --operator-listen needs --operator-profile: every request "
              "on the operator listener is bound to one of its operators",
              file=sys.stderr)
        return None, 2
    from ..mcp.http_guard import Exposure  # noqa: PLC0415

    exposure = Exposure(host=host.strip("[]"), port=int(port),
                        tls_cert=getattr(args, "operator_tls_cert", None),
                        tls_key=getattr(args, "operator_tls_key", None),
                        tls_client_ca=getattr(args, "operator_tls_client_ca", None),
                        allow_hosts=tuple(getattr(args, "operator_allow_host", None)
                                          or ()))
    return {"exposure": exposure, "profile_path": profile,
            "auth": getattr(args, "operator_auth", "bearer"),
            "settle_ms": getattr(args, "profile_settle_ms", 1000)}, None


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
                                     follow_redirects=args.follow_redirects,
                                     long_running=args.long_running)
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
                                   composition=args.composition,
                                   face=getattr(args, "face", "rest"),
                                   component=getattr(args, "component", None))
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

    if args.export_command == "openapi":
        from ..export_openapi import export_openapi  # noqa: PLC0415
        try:
            source = export_openapi(ir, service=args.service)
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


def _run_sourcemap(args: argparse.Namespace) -> int:
    """`revl sourcemap compose` — chain a generated file's map into the
    bundler's (item 459 F2, docs/frontend-assets.md).

    `stdlib/template.rvl` maps rendered text back to the template that produced
    it; a bundler maps its output back to the files it read. When a rendered
    file is one of those, the two maps meet nowhere and a browser stack trace
    stops at the generated file. This composes them into one document.

    Reads exactly the paths on its own command line. A `sources` or
    `sourceMappingURL` entry INSIDE a map is a label that is compared and copied
    as text and never opened: a map is a build artifact, and a composer that
    resolved the paths it names would be a file-read primitive reachable from
    one.
    """
    from ..sourcemap import SourceMapError, compose

    def _read(path_str: str):
        try:
            with open(path_str, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            print(f"error: cannot read {path_str}: {error}", file=sys.stderr)
            return None

    outer = _read(args.map)
    if outer is None:
        return 1

    # `--through` is repeatable so a bundle built from several rendered files
    # chains each of them; the composition is applied in the order given, and
    # each step's output is the next step's outer map.
    for spec in args.through:
        name, _, path_str = spec.partition("=")
        if not path_str:
            name, path_str = None, spec
        inner = _read(path_str)
        if inner is None:
            return 1
        try:
            outer = compose(outer, inner, name)
        except SourceMapError as error:
            print(f"error: {path_str}: {error}", file=sys.stderr)
            return 1

    text = json.dumps(outer, separators=(",", ":"), sort_keys=False)
    if args.output:
        try:
            Path(args.output).write_text(text + "\n", encoding="utf-8")
        except OSError as error:
            print(f"error: cannot write {args.output}: {error}", file=sys.stderr)
            return 1
    else:
        print(text)
    return 0


def _announce_or_reexec_runtime(args) -> None:
    """Issue #1692: never serve with the runtime verbs silently dead. Without
    cordis, re-execute under the repository's runtime venv when it has it
    (this does not return), or else say on stderr which verbs are unavailable;
    the server refuses those verbs by name (`mcp/runtime_gate.py`)."""
    from ..mcp import runtime_gate as _gate  # noqa: PLC0415 — lazy

    target = _gate.reexec_target()
    if target is not None:
        _gate.reexec(target, getattr(args, "raw_argv", None) or sys.argv[1:])
    if not _gate.cordis_importable():
        print(f"revl mcp serve: {_gate.announcement()}", file=sys.stderr,
              flush=True)


# -- issue #1708: `revl act`, the CLI form of `revl_act` -----------------------

def _run_act(args) -> int:
    """`revl act FILES...`: boot the composition under the approval gate, run
    each proposed action read from stdin (one JSON object per line: `key`,
    `method`, `args`) through the same handler as the `revl_act` MCP verb, and
    print one JSON result per line. At end of input print the commit manifest,
    which lists every action, then confirm it with `--commit` or abort.

    Nothing here can approve a ticket, so a class-(c) action stays a ticket and
    never fires. Exit status: 0 when every line was acted on, 1 when a line was
    malformed or refused, or the composition did not boot."""
    from ..mcp import server  # noqa: PLC0415

    session = _act_session(args)
    if session is None:
        return 1
    server.SESSION = session
    failed = False
    for line in sys.stdin:
        if line.strip():
            out = _act_line(server, line)
            failed |= not out.get("ok") and not out.get("approvalRequired")
            print(json.dumps(out, default=str), flush=True)
    print(json.dumps(_act_finish(server, args), default=str), flush=True)
    return 1 if failed else 0


def _act_session(args):
    """A recording session under the gate, with the composition booted; None
    (and the reason on stderr) when it cannot boot."""
    from .._paths import backends_root  # noqa: PLC0415

    backend = backends_root() / "python"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    from ..mcp.approval import ApprovalRequired  # noqa: PLC0415
    from ..mcp.session import Session, SessionError  # noqa: PLC0415

    try:
        ir = compile_files(args.files)
    except RevlError as error:
        print(json.dumps(report(error), indent=2))
        return None
    session = Session()
    session.approval_policy = "auto"
    if getattr(args, "wal", None):
        session._wal_path = args.wal
    try:
        session.load(ir, record=True)
    except ApprovalRequired as exc:
        print(f"error: the composition's activation body reaches a class-(c) "
              f"crossing (ticket {exc.ticket.get('hash')}), and `revl act` "
              f"cannot approve one. Serve it with `revl mcp serve` instead",
              file=sys.stderr)
        return None
    except SessionError as error:
        print(f"error: {error}", file=sys.stderr)
        return None
    return session


def _act_line(server, line: str) -> dict:
    """One proposed action, through the `revl_act` handler."""
    try:
        action = json.loads(line)
    except json.JSONDecodeError as error:
        return server._session_error(f"not a JSON action: {error}")
    if not isinstance(action, dict):
        return server._session_error("an action is a JSON object with `key`, "
                                     "`method` and `args`")
    return server._tool_act(action)


def _act_finish(server, args) -> dict:
    """The commit manifest, then the commit (`--commit`) or the abort."""
    manifest = server._tool_commit({})
    if not manifest.get("ok"):
        return manifest
    if getattr(args, "commit", False):
        done = server._tool_commit_confirm({"hash": manifest["manifest"]["hash"]})
        return {**manifest, "committed": done}
    return {**manifest, "aborted": server._tool_abort({})}

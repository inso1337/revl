"""Argument-parser assembly for the revl CLI (roadmap item 111).

`build_parser()` is the verbatim subcommand tree that `main()` dispatches
over; kept apart so `__main__` reads as parser-assembly + dispatch."""

from __future__ import annotations

import argparse

from ..run import KNOWN_BACKENDS, RUNNABLE_BACKENDS


def build_parser() -> argparse.ArgumentParser:
    """Assemble the full `revl` subcommand parser."""
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

    scaffold = sub.add_parser(
        "scaffold",
        help="generate a typed, holed composition skeleton from a spec (docs/scaffold.md)")
    scaffold.add_argument("--service", required=True, metavar="NAME",
                          help="the service the component provides")
    scaffold.add_argument("--provides", default=None, metavar="KEY",
                          help="the provision key (default: the service, lowercased)")
    scaffold.add_argument("--component", default=None, metavar="NAME",
                          help="the component name (default: <Service>Provider)")
    scaffold.add_argument("--requires", action="append", default=[], metavar="KEY[:Service]",
                          help="an injected dependency; repeatable. A bare KEY defaults "
                               "its service to KEY capitalized")
    scaffold.add_argument("--capabilities", action="append", default=[], metavar="CAP",
                          help="a boundary the component may emit through; repeatable. Only a "
                               "capability whose boundary is injected (--requires) becomes an "
                               "emission bound — an un-injected one stays a hole, never a "
                               "silently widened permission")
    scaffold.add_argument("--method", action="append", default=[], metavar="'name(p: T) -> R'",
                          help="a pure service method; repeatable")
    scaffold.add_argument("--emits", action="append", default=[], metavar="'name(p: T) -> R'",
                          help="an emission service method, bound to the wired capabilities; "
                               "repeatable")
    scaffold.add_argument("--config", action="append", default=[], metavar="name:Type",
                          help="a component config field; repeatable")
    scaffold.add_argument("--resource", default=None, metavar="Type",
                          help="the type of the effect-acquired resource "
                               "(default: <Service>Resource)")
    scaffold.add_argument("--no-effect", action="store_true",
                          help="omit the acquire/undo effect block")
    scaffold.add_argument("-o", "--out", default=None, metavar="PATH",
                          help="write the .rvl skeleton here (default: stdout)")
    scaffold.add_argument("--json", action="store_true",
                          help="print the skeleton, its obligations, and each hole's fill spec "
                               "as one JSON document")

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

    diff_cmd = sub.add_parser(
        "diff",
        help="semantic composition diff: the IR-level structural delta between "
             "two compositions (components added/removed/changed, emissions "
             "gained/lost, provide/require edges added/broken) — the PR-review "
             "tool for agent-generated compositions (docs/revl-diff.md)")
    diff_cmd.add_argument(
        "before", metavar="BEFORE",
        help="the earlier composition: a compiled IR/interchange JSON document "
             "(`revl compile -o` or `revl audit --json`) or a `.rvl` source")
    diff_cmd.add_argument(
        "after", metavar="AFTER",
        help="the later composition (same accepted forms as BEFORE)")
    diff_cmd.add_argument("--json", action="store_true",
                          help="machine-readable delta an agent can consume")

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

    canary_cmd = sub.add_parser(
        "canary",
        help="progressive delivery for one slice: run a candidate on a "
             "designated realm, compare recorded worlds (replay), prove the "
             "revert clean — the other tenants untouched (docs/verified-canary.md)")
    canary_cmd.add_argument("files", nargs="+",
                            help="the running (baseline) composition's .rvl files")
    canary_cmd.add_argument("--candidate", action="append", required=True, metavar="FILE",
                            help="the successor generation of the slice's provider; "
                                 "repeatable")
    canary_cmd.add_argument("--slice", required=True, metavar="REALM",
                            help="the designated slice — a named realm (a tenant, "
                                 "a sandbox)")
    canary_cmd.add_argument("--provider", default=None, metavar="COMPONENT",
                            help="the slice's provider to canary (only needed when "
                                 "the realm serves several)")
    canary_cmd.add_argument("--promote-to", default=None, metavar="BACKEND",
                            help="report a promote (= swap the remainder) admission "
                                 "verdict for this tier")
    canary_cmd.add_argument("--json", action="store_true",
                            help="machine-readable, versioned report document")
    canary_cmd.add_argument("--no-residue-proof", action="store_true",
                            help="skip the runtime teardown proof (static survivors "
                                 "proof only; use where cordis is unavailable)")


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

    quarantine = sub.add_parser(
        "quarantine",
        help="prove an untrusted Str-surface candidate in the wasm sandbox "
             "(item 45): grade it with the gauntlet, then run its lifecycle + "
             "fault battery as a standard component under wasmtime — where an "
             "escape is a trap, not an incident")
    quarantine.add_argument("files", nargs="+")
    quarantine.add_argument("--json", action="store_true",
                            help="machine-readable report")
    quarantine.add_argument(
        "--service", default=None, metavar="NAME",
        help="WIT interface name to group the candidate's Str-surface functions "
             "under (default: the sole declared service, else `Candidate`)")
    quarantine.add_argument(
        "--policy", default=None, metavar="POLICY",
        help="a boundary policy (item 33): with `quarantine required`, the "
             "admission decision reports whether the candidate is admissible")
    quarantine.add_argument(
        "--require-runtime", action="store_true",
        help="fail (exit 3) instead of exiting 0 when wasm-tools/wasmtime are "
             "absent, so the substrate battery could not actually run")

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
    # boundary policy (item 33) for the served session: bounds the agent
    # sandbox and, with `leases enforced`, promotes the item-61 component-lease
    # advisory to an admission refusal. Opt-in — omit for advisory-only leases.
    mcp_serve.add_argument("--policy", default=None, metavar="POLICY",
                           help="a boundary-policy file (item 33) bound to this "
                                "session: its `mcp` sandbox bounds admitted agent "
                                "code, and `leases enforced` refuses a swap that "
                                "would replace a component another operator leases "
                                "(item 61). Omit for advisory-only leases")
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
                     help="target runtime tier (default: py). All tiers run: "
                          f"{', '.join(RUNNABLE_BACKENDS)} — py boots in-process, "
                          "the rest each boot as a separate process over the bridge "
                          "seam; --once for the boot/teardown round-trip; a missing "
                          "runtime is a skip with a reason and a nonzero exit")
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

    metrics_cmd = sub.add_parser(
        "metrics",
        help="capability-aware runtime metrics over a `revl run --trace` JSONL "
             "trace (item 122): emission count by capability, failure count by "
             "G-rule, and average lifecycle duration (docs/revl-metrics.md)")
    metrics_cmd.add_argument(
        "trace", metavar="FILE",
        help="a JSONL causal trace written by `revl run --trace`")
    metrics_cmd.add_argument(
        "--json", action="store_true",
        help="machine-readable metrics document instead of the human table")

    profile_cmd = sub.add_parser(
        "profile",
        help="capability/emission profiling (item 124): diff a component's "
             "DECLARED emission surface against what a `revl run --trace` JSONL "
             "trace actually emitted, flagging over-declaration "
             "(docs/revl-profile.md)")
    profile_cmd.add_argument(
        "composition", metavar="COMPOSITION",
        help="the composition whose declarations to read — a `.rvl` source, a "
             "compiled IR (`revl compile -o`), or an `audit --json` document")
    profile_cmd.add_argument(
        "trace", metavar="FILE",
        help="a JSONL causal trace written by `revl run --trace`")
    profile_cmd.add_argument(
        "--json", action="store_true",
        help="machine-readable profile document instead of the human table")
    profile_cmd.add_argument(
        "--strict", action="store_true",
        help="least-privilege gate: exit nonzero if any component over-declares "
             "an emission the run never exercised")

    attest_cmd = sub.add_parser(
        "attest",
        help="cryptographic attestation of a verified composition (item 127): "
             "sign a portable record that this exact composition was admitted "
             "(canonical IR hash + verdict + guarantees + timestamp), or "
             "--verify one (docs/revl-attest.md)")
    attest_cmd.add_argument(
        "target", metavar="TARGET",
        help="what to attest: a composition (a `.rvl` source, a compiled IR, "
             "or an `audit --json` document). With --verify, the attestation "
             "JSON to check instead")
    attest_cmd.add_argument(
        "--verify", action="store_true",
        help="verify mode: TARGET is an attestation JSON — check its signature "
             "(and, with --against, that the composition still matches). Exits "
             "nonzero if the attestation is invalid")
    attest_cmd.add_argument(
        "--against", metavar="COMPOSITION", default=None,
        help="with --verify: the composition to re-hash and check the "
             "attestation against (a `.rvl` source or compiled IR). Omit to "
             "check only the signature over the attestation's embedded hash")
    attest_cmd.add_argument(
        "--key", metavar="PATH", default=None,
        help="the signing/verifying key file. Falls back to the "
             "REVL_ATTEST_KEY_FILE (a path) or REVL_ATTEST_KEY (the secret) "
             "environment variables. Never hardcoded")
    attest_cmd.add_argument(
        "--signer", metavar="NAME", default=None,
        help="an optional signer label recorded in (and signed into) the "
             "attestation; falls back to the REVL_ATTEST_SIGNER env var")
    attest_cmd.add_argument(
        "--json", action="store_true",
        help="machine-readable output: the attestation document, or the "
             "verify verdict as JSON")

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

    # `revl truc <verb> ...` — a namespaced door onto the standalone `truc`
    # binary (roadmap item 136, slice S2). This is a pure passthrough: the tail
    # after `truc` is handed verbatim to truc's own launcher (`revl.truc:main`,
    # the same entry point the `truc` console script calls), so every verb truc
    # grows — `add`/`rm`/`assemble`/`ship` — works through `revl truc <verb>`
    # without this dispatch knowing any of them. REMAINDER captures the tail
    # untouched (flags included), so `revl truc add X` == `truc add X`.
    truc_cmd = sub.add_parser(
        "truc",
        help="the revl component manager — `revl truc <verb>` is the namespaced "
             "form of the standalone `truc <verb>` (add/rm/assemble/ship); the "
             "tail is passed through unchanged (docs/truc.md)")
    truc_cmd.add_argument("truc_args", nargs=argparse.REMAINDER, metavar="...",
                          help="the truc verb and its arguments, forwarded as-is")
    return parser

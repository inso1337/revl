"""Argument-parser assembly for the revl CLI (roadmap item 111).

`build_parser()` is the verbatim subcommand tree that `main()` dispatches
over; kept apart so `__main__` reads as parser-assembly + dispatch."""

from __future__ import annotations

import argparse

from ..run import KNOWN_BACKENDS, RUNNABLE_BACKENDS

# The backends `revl bundle` can emit (the emitter package names under
# backends/). Kept as a literal here so parser assembly stays import-light —
# it mirrors `revl.bundle.DEFAULT_BACKENDS`, which is the authority.
BUNDLE_BACKENDS = ("python", "typescript", "rust", "java", "go", "wasm")


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
    cmd.add_argument("--taint-strict", action="store_true",
                     help="derive taint sinks and sources with no annotation (item 249, "
                          "Slice D): a shell/exec/terminal-scoped crossing refuses "
                          "untrusted input, and a web/net/fs/model/input emission mints "
                          "its origin. Additive — a program that already passes without "
                          "it is unaffected")

    exp = sub.add_parser("explain", help="what a diagnostic code means and how to fix it")
    exp.add_argument("code", help="a diagnostic code, e.g. G4 (case-insensitive)")
    exp.add_argument("--json", action="store_true", help="machine-readable output")

    grammar = sub.add_parser(
        "grammar",
        help="the revl surface syntax, small enough to keep in a prompt")
    grammar.add_argument(
        "--prompt", action="store_true",
        help="print the dense, complete, prompt-pinnable grammar (roadmap item "
             "346; also shipped as docs/syntax-2.0.prompt.txt) instead of the "
             "short human-readable summary")

    # item 296: propose a safe adapter between a consumer's required service and
    # a candidate's provided service (proposed, not silent).
    adapt = sub.add_parser(
        "adapt",
        help="propose a safe contract adapter between a required and a "
             "provided service (item 296)")
    adapt.add_argument("need", help=".rvl file declaring the required service")
    adapt.add_argument("candidate",
                       help=".rvl file declaring the candidate's provided service")
    adapt.add_argument("--need-service", default=None,
                       help="name the required service (default: the sole one)")
    adapt.add_argument("--candidate-service", default=None,
                       help="name the candidate service (default: the sole one)")
    adapt.add_argument("--adapt", default=None, metavar="JSON_FILE",
                       help="opt-in map `D` (defaults, drops, merges, pairings)")
    adapt.add_argument("--emit", action="store_true",
                       help="also render the synthesized adapter .rvl source "
                            "(the artifact to commit)")
    adapt.add_argument("--name", default="Adapter",
                       help="component name for --emit (default: Adapter)")
    adapt.add_argument("--provide-key", default=None,
                       help="provided key for --emit (default: the need "
                            "service name, lowercased)")
    adapt.add_argument("--require-key", default="backing",
                       help="alias the candidate is required under for --emit "
                            "(default: backing)")

    doctor = sub.add_parser(
        "doctor",
        help="diagnose each backend tier, runtime and dependency (OK/WARN/"
             "MISSING + version), then smoke-test every available tier")
    doctor.add_argument("--json", action="store_true",
                        help="machine-readable report (for an agent)")
    doctor.add_argument("--no-smoke", action="store_true",
                        help="skip the per-tier compile+boot smoke test (report only)")
    doctor.add_argument("--smoke-timeout", type=int, default=None, metavar="SECONDS",
                        help="per-tier smoke-test timeout (default: 90)")

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

    # item 426 S1: the row table of a declared composition.
    composition = sub.add_parser(
        "composition",
        help="resolve a composition document's ROW TABLE (labels, claims, "
             "config), header-only")
    composition.add_argument("file", help="the .rvl document declaring the composition")
    composition.add_argument(
        "--json", action="store_true",
        help="print the row table as JSON instead of the ROWS/WIRING panels")
    composition.add_argument(
        "--admit", action="store_true",
        help="also COMPILE the rows the table names and print the resulting "
             "load order (resolution alone lowers no component body)")
    composition.add_argument(
        "--root", default=None, metavar="DIR",
        help="the project root row provenance and origins are recorded "
             "against (default: the working directory)")
    composition.add_argument(
        "--set", action="append", default=[], metavar="@ROW.FIELD=VALUE",
        dest="overrides",
        help="item 426 S2, the INVOCATION OVERLAY (the last level): override "
             "one config value of one row. Values only, never structure — it "
             "cannot add, remove or replace a row — and the value is typed "
             "against the component's declared `config` exactly like every "
             "other one (repeatable)")

    # item 426 S2: layers and the fold.
    layer = sub.add_parser(
        "layer",
        help="composition LAYERS (docs/composition-layers.md): fold a "
             "composition's declared stack and site layers into its row table, "
             "header-only")
    layer_sub = layer.add_subparsers(dest="layer_command", required=True)
    layer_check = layer_sub.add_parser(
        "check",
        help="resolve every row id and render the folded table with each row's "
             "layer provenance, without lowering any component body")
    layer_check.add_argument(
        "file", help="the .rvl document declaring the base composition")
    layer_check.add_argument(
        "--json", action="store_true",
        help="the folded row table as JSON, provenance included")
    layer_check.add_argument(
        "--root", default=None, metavar="DIR",
        help="the project root provenance and origins are recorded against")
    layer_check.add_argument(
        "--set", action="append", default=[], metavar="@ROW.FIELD=VALUE",
        dest="overrides", help="the invocation overlay (see `revl composition`)")

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
    audit.add_argument(
        "--placement", metavar="PLACEMENT", default=None,
        help="a TOML/JSON placement map: also print the item-411 sandbox "
             "envelope per sandboxed process: the fs/net grant, the effective "
             "reach of each seam-served key, and the externs the [sandbox.needs] "
             "table vouches (claimed, unverified). Human output only.")
    # item 309: the replay-class view over the recovery surface.
    audit.add_argument(
        "--recovery", action="store_true", default=None,
        help="also print the item-309 recovery view: every inverse, deferred "
             "emission, and compensation with its replay class (free / fenced / "
             "human-finish) and idempotency register. Human output only.")
    # item 290: the confidence/evidence admission inputs, so the `--policy` gate
    # can see a component's item-293 evidence bundle. Absent unless the policy
    # carries evidence rules; a policy with none is byte-identical.
    audit.add_argument(
        "--evidence", metavar="DIR", default=None,
        help="a component entry directory holding an `evidence/` bundle (item "
             "293) for the single audited composition, so a `requires evidence` "
             "rule can threshold its recorded facts (item 290)")
    audit.add_argument(
        "--key", metavar="PATH", default=None,
        help="an attestation verification key, so `attestation valid` clauses "
             "verify against the rebuilt IR (item 290; keyless `valid` fails "
             "closed)")
    audit.add_argument(
        "--trusted-publisher", action="append", default=[], metavar="ID",
        dest="trusted_publisher",
        help="a publisher id in the operator trust set, for `publisher trusted` "
             "clauses (repeatable; item 290)")

    # item 290: `revl policy evaluate` — the dry-run explain verb. Runs the SAME
    # `policy.evaluate` (one comparison site) and reports, per component, which
    # rules select it and which clauses pass/fail with the recorded fact vs the
    # threshold; never admits, refuses, or mutates.
    policy_cmd = sub.add_parser(
        "policy", help="boundary/evidence policy tools (item 33/290)")
    policy_sub = policy_cmd.add_subparsers(dest="policy_command", required=True)
    pol_eval = policy_sub.add_parser(
        "evaluate",
        help="dry-run: report per rule which clauses pass/fail and why "
             "(fact vs threshold), for a policy over a composition (item 290)")
    pol_eval.add_argument("policy_file", metavar="POLICY",
                          help="the boundary policy file (DSL or JSON)")
    pol_eval.add_argument("files", nargs="*", metavar="PROGRAM.rvl",
                          help="the composition source(s) to evaluate the "
                               "policy against")
    pol_eval.add_argument("--json", action="store_true",
                          help="machine-readable per-clause verdicts")
    pol_eval.add_argument("--component", metavar="NAME", default=None,
                          help="narrow the report to one component")
    pol_eval.add_argument("--evidence", metavar="DIR", default=None,
                          help="a component entry directory holding an "
                               "`evidence/` bundle for a bare-source component "
                               "(item 293)")
    pol_eval.add_argument("--registry", metavar="DIR", default=None,
                          help="evaluate a published registry entry instead of "
                               "a bare source (with --candidate)")
    pol_eval.add_argument("--candidate", metavar="NAME", default=None,
                          help="the registry entry name to evaluate")
    pol_eval.add_argument("--trusted-publisher", action="append", default=[],
                          metavar="ID", dest="trusted_publisher",
                          help="a publisher id in the operator trust set "
                               "(repeatable)")
    pol_eval.add_argument("--key", metavar="PATH", default=None,
                          help="an attestation verification key")
    pol_eval.add_argument("--mcp-scope", action="append", default=[],
                          metavar="COMPONENT",
                          help="treat COMPONENT as MCP/agent-admitted; `*` = "
                               "every component")

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

    # `revl changelog --from OLD --to NEW` (item 261) - the release note is
    # computed, not written. Its own two-input loader (like `diff`), so it is
    # routed before the shared single-source compile step.
    changelog_cmd = sub.add_parser(
        "changelog",
        help="derive a release note from the structural delta between two "
             "compositions: authority widenings and the item-64 semver bump "
             "lead, every line traces to a differ fact (item 261)")
    changelog_cmd.add_argument(
        "--from", dest="from_", metavar="OLD", required=True,
        help="the earlier composition: a compiled IR/interchange JSON document "
             "(`revl compile -o` or `revl audit --json`) or a `.rvl` source")
    changelog_cmd.add_argument(
        "--to", dest="to", metavar="NEW", required=True,
        help="the later composition (same accepted forms as --from)")
    changelog_cmd.add_argument(
        "--format", dest="format", choices=("markdown", "json", "plain"),
        default="markdown",
        help="the output form: `markdown` (the stable release-note skeleton, "
             "default), `json` (the structured document a registry or bot "
             "consumes), or `plain` (the skeleton with no Markdown markup)")
    changelog_cmd.add_argument("--json", action="store_true",
                               help="alias for `--format json`; the structured "
                                    "changelog document a registry or bot can "
                                    "consume")
    changelog_cmd.add_argument(
        "--no-semver", action="store_true",
        help="skip the interface-diff semver headline (structural + authority "
             "changelog only); implied when an input lacks the interface table")
    changelog_cmd.add_argument(
        "--current-version", metavar="X.Y.Z", default=None,
        help="the earlier composition's declared version; when given, the "
             "computed next version is printed in the headline")
    changelog_cmd.add_argument(
        "--title", metavar="TEXT", default=None,
        help="an opaque header line for the Markdown note (e.g. a version and "
             "date); never enters a derived line")

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
                           "siblings at each (py tier). With `--backend all`, "
                           "sweep every runtime whose toolchain is present and "
                           "assert they agree — residue-free on every tier "
                           "(docs/fault-tests.md §10)")
    test.add_argument("--mock-requires", action="store_true",
                      help="run every `lifecycle test` in mock world: each unmet "
                           "`requires` is filled by an auto-generated mock provider "
                           "(item-37-typed, seeded; emissions recorded-not-crossed), "
                           "so a consumer boots with zero real providers "
                           "(py tier; docs/auto-mocks.md)")
    test.add_argument("--schedule-seed", type=int, default=None, metavar="SEED",
                      help="schedule testing: replay one seeded interleaving of "
                           "the composition's concurrent lifecycle steps and "
                           "check residue / deadlock / stable-final-state / "
                           "teardown / use-after-withdrawal — reproducible from "
                           "the seed (py tier; docs/design/295-schedule-testing.md)")
    test.add_argument("--schedule-seeds", type=int, default=None, metavar="N",
                      help="schedule testing: sample N seeded interleavings "
                           "(a random walk over the schedule space) plus the "
                           "canonical sequential baseline, and report any "
                           "interleaving that violates a property (py tier; "
                           "docs/design/295-schedule-testing.md)")

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
    # auto-approve policy (item 246): the second orthogonal gate. `auto` proceeds
    # silently on class (a) (witnessed-revertible), enumerates class (b) (deferred)
    # at commit, and prompts per call on class (c) (an irreversible emission with
    # no checked inverse). Off by default — omit for byte-identical behaviour.
    mcp_serve.add_argument("--approval-policy", default=None, metavar="MODE",
                           choices=("auto",),
                           help="enable the auto-approve policy (item 246): class "
                                "(a)/(b) crossings auto-approve, class (c) prompts "
                                "per call via the ticket two-step. Requires "
                                "`record: true` at load. Omit for no policy "
                                "(today's behaviour)")
    # roadmap 425 F3 / 427 F5: whether an approved crossing's CALLER-SUPPLIED
    # resource value (`host=`, `path=`, `table=`) is written into the durable
    # cross-session approval WAL. Defaults to `withheld` — an operator who never
    # reads this flag does not silently persist somebody else's values in
    # plaintext; `bound` opts into recording them, which is what lets the
    # distiller fold a series of approvals into a rule that NAMES the target
    # (item 251's N1). The ticket discloses that a target is caller-supplied
    # under both, so the operator's yes is informed either way.
    mcp_serve.add_argument("--approval-record-values", default="withheld",
                           choices=("bound", "withheld"),
                           help="whether an approved class-(c) crossing's "
                                "caller-supplied resource value is recorded in "
                                "the durable approval log. `withheld` (default) "
                                "records it as UNRECORDED, keeping caller values "
                                "out of the cross-session WAL; the cost is that "
                                "such approvals no longer fold into a distilled "
                                "rule. `bound` records it verbatim so a rule can "
                                "name the target (item 251 N1). An author-written "
                                "literal target is recorded under both")
    # authoring trust: whether the AGENT driving this server may author host
    # code, and what filesystem it may name. Defaults CLOSED — an inline `@py`
    # body sent to revl_load/check/swap used to compile and RUN as host Python
    # with no operator in the loop (docs/mcp-bridge.md, items 329/334).
    mcp_serve.add_argument("--author-trust", default="untrusted",
                           choices=("untrusted", "trusted"),
                           help="how much to trust the agent on the other end as "
                                "an AUTHOR. `untrusted` (default) compiles its "
                                "source under the item-329 untrusted-author "
                                "profile: it may compose granted services but may "
                                "neither declare nor reach an `extern`/host block. "
                                "`trusted` lets it author host code, and stamps "
                                "every class-(c) ticket with the unreviewed host "
                                "bodies a yes would also run")
    mcp_serve.add_argument("--provider", action="append", default=[],
                           metavar="MODULE.rvl",
                           help="an OPERATOR-sanctioned host-code module the "
                                "untrusted agent may compose the services of "
                                "(repeatable). The granted-providers map item "
                                "334's `Gate.propose` uses: the operator writes "
                                "the host body, the agent wires it")
    mcp_serve.add_argument("--grant", action="append", default=[],
                           metavar="SERVICE",
                           help="a service name an admitted turn may reach "
                                "(repeatable). Naming any turns on the item-329 "
                                "reach allowlist; omit them all and reach is not "
                                "bounded by this flag")
    mcp_serve.add_argument("--root", action="append", default=[], metavar="DIR",
                           help="a directory the agent's path arguments (`files`, "
                                "`candidateFiles`, `baselineFiles`, `traceFile`, "
                                "`registry`) may name (repeatable). Defaults to "
                                "the directory the server was started in; anything "
                                "outside is refused before it is read")
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
    imp_api.add_argument(
        "--compensate", action="append", default=[], metavar="OP",
        help="item 254: promote a write to a COMPENSATE-grade network effect — "
             "attach a best-effort, audit-surface reversal to its emission. The "
             "verb picks the route: `PUT` restores the GET preimage, `DELETE` "
             "recreates from it, `POST` deletes what it created. Hard error on "
             "`PATCH`. Pair with `--preimage`/`--undo`/`--undo-key`. Repeatable")
    imp_api.add_argument(
        "--preimage", action="append", default=[], metavar="OP=GETOP",
        help="item 254: name the safe `GET` operation a compensate-grade `PUT` or "
             "`DELETE` reads its preimage from. Repeatable")
    imp_api.add_argument(
        "--undo", action="append", default=[], metavar="OP=UNDOOP",
        help="item 254: name the operation the reversal issues — the `PUT` that "
             "writes the preimage back (the default for a `PUT`), the create a "
             "`DELETE` recreates through, or the delete that removes what a "
             "`POST` created. Repeatable")
    imp_api.add_argument(
        "--undo-key", action="append", default=[], metavar="OP=FIELD",
        dest="undo_key",
        help="item 254: for a compensate-grade `POST`, the REQUIRED response "
             "field naming the resource it created, which the reversal delete is "
             "addressed by. Without it the compensation is unkeyed and refused. "
             "Repeatable")
    imp_api.add_argument(
        "--if-match", action="append", default=[], metavar="OP", dest="if_match",
        help="item 254: assert a compensate-grade endpoint exposes a version/ETag "
             "token, so the reversal issues under `If-Match` (`If-None-Match: *` "
             "for a recreate) and fails loudly on a racing writer (else it is "
             "best-effort-may-clobber). Repeatable")
    imp_api.add_argument(
        "--require-if-match", action="store_true", dest="require_if_match",
        help="item 254: refuse a compensate-grade promotion on any endpoint that "
             "claims no version/ETag token, instead of emitting a "
             "best-effort-may-clobber reversal for it")
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

    # `revl import a2a` — an A2A 1.0.0 Agent Card's skill surface
    # (docs/import-a2a.md, roadmap item 439 slice 1). Own additive block.
    imp_a2a = imp_sub.add_parser(
        "a2a",
        help="turn an A2A 1.0.0 Agent Card into revl source (docs/import-a2a.md)")
    imp_a2a.add_argument("file", help="an A2A 1.0.0 Agent Card (.json)")
    imp_a2a.add_argument("--backend", default="ts", choices=("ts", "py"),
                         help="host block backend for the generated externs "
                              "(default: ts)")
    imp_a2a.add_argument("--service", default=None,
                         help="generated service name (default: from the card's "
                              "`name`)")
    imp_a2a.add_argument(
        "--allow-plaintext", action="store_true", dest="allow_plaintext",
        help="import a card whose `url` is plaintext `http` (a loopback "
             "development agent, say). Refused without this, because an A2A "
             "peer sits outside the composition's trust boundary and everything "
             "crossing to it is authority leaving the process. The generated "
             "header records that the flag was used")
    imp_a2a.add_argument(
        "--follow-redirects", action="store_true", dest="follow_redirects",
        help="let the generated crossing follow a SAME-ORIGIN 307 or 308 "
             "redirect. Off by default, because the endpoint is part of what "
             "the file declares and what the composition admits: a redirect to "
             "another host makes the derived `net.<host>` reach bound stop "
             "describing where the crossing can reach, and a 301/302/303 "
             "re-issues the POST as a GET with the body dropped. Those stay "
             "refused even with this flag; only the two method-preserving codes "
             "on the declared origin are followed")
    imp_a2a.add_argument("-o", "--output", default=None,
                         help="output path (default: stdout)")
    imp_a2a.add_argument("--json-diagnostics", action="store_true",
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
    serve.add_argument("--env", default=None,
                       help="TOML/JSON file of flat `name = value` environment values, injected into the composition's `boot` component — its `config {}` block is the environment contract, and an undeclared key, a missing required field or a value outside a declared `under`/`in` bound refuses the boot (item 350)")
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
    run.add_argument("--env", default=None,
                     help="TOML/JSON file of flat `name = value` environment values, injected into the composition's `boot` component — its `config {}` block is the environment contract, and an undeclared key, a missing required field or a value outside a declared `under`/`in` bound refuses the boot (item 350)")
    run.add_argument("--policy", default=None, metavar="POLICY",
                     help="boundary policy file (item 33). With --backend wasm it "
                          "enforces the item-289 least-authority chain (host "
                          "imports subset-of declared caps subset-of policy-allowed) "
                          "before booting; a wasm cell exceeding the allow-list is "
                          "refused (docs/boundary-policy.md)")
    run.add_argument("--watch", action="store_true",
                     help="watch the sources and recompile on change; a rejected edit is refused, the run keeps going")
    run.add_argument("--record", action="store_true",
                     help="record the effect accumulator so the REPL can step "
                          "backwards over it (`:timeline`, `:back k`) — see docs/replay.md")
    run.add_argument("--estop-latch", default=None, metavar="FILE",
                     help="watch FILE for an operator E-Stop (item 443). While "
                          "armed, every boundary-crossing seam checks the latch, "
                          "so `revl estop --latch FILE` from another terminal "
                          "halts this run immediately — no unwind, an honest "
                          "in-flight inventory instead. With --placement the "
                          "conductor watches it too and halts every process, "
                          "naming the ones on tiers that have no E-Stop seam "
                          "and were only killed. Equivalent to the "
                          "REVL_ESTOP_LATCH environment variable")
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
    # re-establishing the approval posture on roll-forward (item 246): a snapshot
    # taken under `--approval-policy`/`--policy` refuses to restore into a
    # policy-less session, because that would replay a once-approved class-(c)
    # activation crossing UNPROMPTED. Pass the SAME posture the original serve had
    # so the gate re-arms and the crossing re-prompts (or is refused) as on first
    # boot (docs/crash-recovery.md).
    recover.add_argument("--approval-policy", default=None, metavar="MODE",
                         choices=("auto",),
                         help="on --restore, re-arm the auto-approve policy (item "
                              "246) the snapshot was taken under, so a class-(c) "
                              "activation crossing re-prompts on recovery instead "
                              "of firing unprompted. Required when the snapshot "
                              "records an approval policy")
    recover.add_argument("--policy", default=None, metavar="POLICY",
                         help="the boundary-policy file (item 33). On --restore it "
                              "re-binds the posture the snapshot was taken under so "
                              "the `requires approval` gate re-arms; a `recovery may "
                              "re-issue owed emissions` rule additionally turns on "
                              "the item-440 re-issue seam (off by default)")
    recover.add_argument("--json", action="store_true", help="machine-readable output")

    estop = sub.add_parser(
        "estop",
        help="E-STOP (item 443): the operator's emergency halt. Arm the latch a "
             "running composition watches, so it stops dispatching NEW boundary "
             "crossings immediately and reports what was in flight — instead of "
             "the graceful two-phase LIFO unwind every other stop performs "
             "(docs/design/443-estop.md)")
    estop.add_argument("--latch", default=None, metavar="FILE",
                       help="the latch file the running process watches "
                            "(`revl run --estop-latch FILE`, or the ambient "
                            "REVL_ESTOP_LATCH). Required unless --wal is given, "
                            "which derives FILE.estop")
    estop.add_argument("--wal", default=None, metavar="FILE",
                       help="the running session's write-ahead log. Derives the "
                            "latch as FILE.estop when --latch is omitted, and "
                            "names the log `revl recover` reconciles from")
    estop.add_argument("--reason", default="operator halt", metavar="TEXT",
                       help="why the button was hit. Carried into the halt "
                            "record and into every residue record it produces")
    estop.add_argument("--operator", default=None, metavar="TOKEN",
                       help="the operator token accountable for the halt. An "
                            "E-Stop is an operator authority (item 55's `estop` "
                            "verb): it is never something a composition or an "
                            "agent may invoke on itself")
    estop.add_argument("--report", action="store_true",
                       help="read the latch back and print what was halted, "
                            "WITHOUT arming anything or touching the world")
    estop.add_argument("--clear", action="store_true",
                       help="remove the latch so a FRESH process may boot. This "
                            "is not a resume: the halted instance stays dead and "
                            "its stranded entries stay owed until `revl recover` "
                            "reconciles them (item 443, open question 3)")
    estop.add_argument("--json", action="store_true",
                       help="machine-readable output")
    branch_cmd = sub.add_parser(
        "branch",
        help="session branch lineage over durable write-ahead logs (item 250): "
             "what a WAL is (a branch, a parent frozen at k, or neither), the "
             "branch tree across several WALs, and — with --at — the fork "
             "partition of a recorded tail "
             "(docs/design/250-session-branching.md)")
    branch_cmd.add_argument(
        "--wal", required=True, action="append", metavar="FILE",
        help="a write-ahead log; repeat it to reconstruct the branch tree across "
             "a parent and its branches")
    branch_cmd.add_argument(
        "--at", type=int, default=None, metavar="SEQ",
        help="instead of the lineage, enumerate the fork partition of the tail "
             "above this WAL position: what a fork here would put back, what "
             "already crossed and cannot be undone, and what it would refuse. "
             "-1 is the whole recorded tail. Requires a single --wal; runs "
             "nothing and rewinds nothing")
    branch_cmd.add_argument("--json", action="store_true",
                            help="machine-readable output")

    compare_cmd = sub.add_parser(
        "compare",
        help="compare two recorded session histories that share a fork point "
             "(item 250): what each did after diverging, and what a comparison "
             "of durable logs cannot yet say "
             "(docs/design/250-session-branching.md)")
    compare_cmd.add_argument("left", metavar="LEFT.wal",
                             help="one session's write-ahead log")
    compare_cmd.add_argument("right", metavar="RIGHT.wal",
                             help="the other session's write-ahead log")
    compare_cmd.add_argument("--json", action="store_true",
                             help="machine-readable output")

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

    trace_cmd = sub.add_parser(
        "trace",
        help="the causal trace with the model hop as a first-class span (item "
             "121): per model completion, the model / tokens / cost / latency / "
             "attempts-vs-ceiling / produced edge / verifying G-rule "
             "(docs/design/121-revl-trace.md)")
    trace_cmd.add_argument(
        "trace", metavar="FILE",
        help="a JSONL causal trace written by `revl run --trace`")
    trace_cmd.add_argument(
        "--json", action="store_true",
        help="the machine-readable trace document instead of the human view")
    trace_cmd.add_argument(
        "--component", metavar="NAME", default=None,
        help="only the hops of this component")
    trace_cmd.add_argument(
        "--model", action="store_true",
        help="only model hops (the LLM view; the default view already is)")
    trace_cmd.add_argument(
        "--otel", action="store_true",
        help="emit the trace through the OTel SDK (delegates to revl.otel)")

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
    profile_cmd.add_argument(
        "--patch", "--minimize", dest="patch", action="store_true",
        help="repair mode (item 307): instead of the profile, PROPOSE the "
             "least-authority `emission[...]` each over-declaring component "
             "should declare (narrowed to the observed reach, never past a `*`). "
             "Printed, never applied: apply it and re-run the gate. Combine "
             "with --json for an agent-consumable patch document")

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

    # `revl bundle <sources...> --out DIR` / `revl verify <bundle>` - the
    # reproducible production bundle (roadmap item 305, docs/bundle.md). `bundle`
    # assembles source + IR + lock + per-backend emitted + policy + attestation
    # + gauntlet (+ optional topology) into one directory; `verify` recompiles it
    # and proves it rebuilds bit-for-bit, tier by tier.
    bundle_cmd = sub.add_parser(
        "bundle",
        help="assemble a reproducible production bundle: source, IR, lock, "
             "per-backend emitted artifacts, policy, attestation and gauntlet "
             "evidence in one .revlbundle directory (item 305, docs/bundle.md)")
    bundle_cmd.add_argument("files", nargs="+",
                            help=".rvl sources to bundle (the whole composition)")
    bundle_cmd.add_argument("--out", required=True, metavar="DIR",
                            help="the bundle directory to write (e.g. app.revlbundle)")
    bundle_cmd.add_argument(
        "--backend", action="append", default=[], metavar="BACKEND",
        choices=list(BUNDLE_BACKENDS),
        help="a backend to emit into the bundle; repeatable. Omit to emit every "
             f"backend ({', '.join(BUNDLE_BACKENDS)}); an emitter that refuses "
             "this IR is recorded as skipped, not a failure")
    bundle_cmd.add_argument(
        "--topology", default=None, metavar="PLACEMENT",
        help="a placement/topology map (TOML or JSON) to carry in the bundle as "
             "topology.json; omit for a single-process bundle")
    bundle_cmd.add_argument("--json", action="store_true",
                            help="print the bundle path and its runtime-manifest as JSON")

    # `revl emit --target temporal` (roadmap item 253): render a backend's
    # emitter in a chosen TARGET, a dimension orthogonal to `--backend`. A target
    # selects a rendering of the backend's emitter (its native runtime by
    # default); `temporal` is the Temporal TS-SDK rendering of the typescript
    # emitter (docs/design/253-temporal-target.md §4). This is NOT a new tier.
    emit_cmd = sub.add_parser(
        "emit",
        help="emit a backend's source in a chosen target (e.g. --target temporal "
             "renders the typescript emitter for the Temporal TS SDK, item 253)")
    emit_cmd.add_argument("files", nargs="+",
                          help=".rvl sources to compile and emit")
    emit_cmd.add_argument("--backend", default="typescript",
                          choices=list(BUNDLE_BACKENDS),
                          help="the backend emitter to render (default: typescript)")
    emit_cmd.add_argument("--target", default=None, metavar="TARGET",
                          help="a rendering of the backend's emitter, orthogonal "
                               "to --backend. `temporal` renders the typescript "
                               "emitter for the Temporal TS SDK (item 253); omit "
                               "for the backend's native runtime")
    emit_cmd.add_argument("-o", "--output", default=None, metavar="PATH",
                          help="output path (default: stdout)")

    verify_cmd = sub.add_parser(
        "verify",
        help="recompile a .revlbundle and prove it rebuilds bit-for-bit: source "
             "and IR hashes match, deps are locked, emitted artifacts correspond "
             "to each backend, capabilities match policy, evidence is present "
             "(item 305, docs/bundle.md)")
    verify_cmd.add_argument("bundle", metavar="BUNDLE",
                            help="a bundle directory written by `revl bundle`")
    verify_cmd.add_argument("--json", action="store_true",
                            help="machine-readable tier-by-tier report")

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
             "form of the standalone `truc <verb>` (add/rm/assemble/ship/"
             "reproduce); the tail is passed through unchanged (docs/truc.md)")
    truc_cmd.add_argument("truc_args", nargs=argparse.REMAINDER, metavar="...",
                          help="the truc verb and its arguments, forwarded as-is")
    return parser

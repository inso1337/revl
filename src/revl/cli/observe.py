"""Per-command CLI handlers: read-only inspection (explain / diff / why / dash / metrics / profile / attest / historical query).

Pure move — per-command CLI handlers, byte-identical behavior; see revl.__main__ for dispatch.
"""

from __future__ import annotations

import json
import sys

from ..compiler import compile_files
from ..diagnostics import explain
from ..errors import RevlError


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


def _run_why(args) -> int:
    """`revl why <component> --trace run.jsonl` — the runtime companion to the
    compile-time why-traces: the cause chain behind a component's recorded
    lifecycle transition, and (with --check) the prediction-vs-actuality
    oracle (docs/why-runtime.md)."""
    from .. import why_runtime

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
    from .. import dash, why_runtime  # noqa: PLC0415

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
        from ..policy import load_policy, PolicyError  # noqa: PLC0415
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


def _run_metrics(args) -> int:
    """`revl metrics <run.jsonl> [--json]` — capability-aware runtime metrics
    over a recorded lifecycle trace (docs/revl-metrics.md, roadmap item 122):
    emission count by capability, failure count by G-rule, and average
    lifecycle duration. A read-only rollup, not a gate — it always exits 0. A
    v1 trace (or one missing `ts`) degrades the duration metric to
    `unavailable` rather than failing."""
    from .. import metrics as _metrics  # noqa: PLC0415

    try:
        computed = _metrics.metrics_from_file(args.trace)
    except (OSError, ValueError) as error:
        print(f"error: cannot read trace {args.trace}: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(computed, indent=2))
    else:
        print(_metrics.render(computed))
    return 0


def _run_trace(args) -> int:
    """`revl trace <run.jsonl> [--json] [--component] [--model] [--otel]` — the
    causal trace with the model hop as a first-class span (item 121,
    docs/design/121-revl-trace.md). A read-only projection, not a gate — it
    always exits 0. `--otel` delegates to :mod:`revl.otel` (the same OTel SDK
    export the `--otel` flag drives elsewhere); the LLM `llm` payload rides onto
    the emit span with GenAI-convention names and a `model-produced` SpanLink
    that appears only when the fiber-local value-flow token proved the edge."""
    from .. import trace as _trace  # noqa: PLC0415

    try:
        events = _trace.read_trace(args.trace)
    except (OSError, ValueError) as error:
        print(f"error: cannot read trace {args.trace}: {error}", file=sys.stderr)
        return 1

    if getattr(args, "otel", False):
        from .. import otel as _otel  # noqa: PLC0415
        if args.json or not _otel.otel_available():
            spans = _otel.build_spans(events)
            print(json.dumps([_otel._model_to_dict(s) for s in spans], indent=2))
        else:
            result = _otel.export_to_otel(events)
            if not result.get("exported"):
                print(result.get("reason", "export failed"), file=sys.stderr)
            else:
                print(f"exported {result['spans']} spans to OpenTelemetry",
                      file=sys.stderr)
        return 0

    doc = _trace.compute_trace(events)
    doc = _trace.filter_document(
        doc, component=getattr(args, "component", None),
        model_only=getattr(args, "model", False))

    if args.json:
        print(json.dumps(doc, indent=2))
    else:
        print(_trace.render(doc))
    return 0


def _run_profile(args) -> int:
    """`revl profile <composition> <run.jsonl> [--json] [--strict]` — the
    least-privilege companion to `revl audit`/`revl metrics` (item 124). Diffs a
    component's declared emission surface (the static G8 walk) against the
    emissions a recorded run exercised (`emit` events), and flags
    over-declaration: a declared emission the run never used.

    Descriptive by default (exit 0 — a profile is not a gate). `--strict` turns
    it into an authority-drift gate: nonzero when anything is over-declared. A
    composition/trace mismatch (an emission used but not declared, or an emitter
    with no declared surface) is surfaced as a warning either way, never faked."""
    from .. import profile as _profile  # noqa: PLC0415

    try:
        computed = _profile.profile_from_files(args.composition, args.trace)
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"error: cannot profile: {error}", file=sys.stderr)
        return 1

    # --patch (item 307): reuse the same declared-vs-observed profile, but emit
    # the proposed least-authority repair patch rather than the profile itself.
    # It is a SUGGESTION, never a gate, so it always exits 0 (even with --strict:
    # the patch is what you apply to *clear* a strict failure, not the check).
    if getattr(args, "patch", False):
        patch = _profile.compute_repair_patch(computed)
        if args.json:
            print(json.dumps(patch, indent=2))
        else:
            print(_profile.render_patch(patch))
        return 0

    if args.json:
        print(json.dumps(computed, indent=2))
    else:
        print(_profile.render(computed))

    if args.strict and computed["summary"]["overDeclaredKeys"] > 0:
        return 1
    return 0


def _run_attest(args) -> int:
    """`revl attest <comp> [--json] [--key ...]` — sign a portable record that
    a composition was admitted; `revl attest --verify <att> [--against comp]`
    checks one (docs/revl-attest.md, roadmap item 127).

    Sign mode exits 0 on success. Verify mode is a check: it exits nonzero when
    the attestation is invalid (bad signature/key, tampered, or — with
    --against — the composition changed)."""
    from .. import attest as _attest  # noqa: PLC0415
    from ..composition_diff import load_composition  # noqa: PLC0415 — READ-ONLY IR loader

    try:
        key = _attest.resolve_key(args.key)
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.verify:
        try:
            att = _attest.load_attestation(args.target)
        except RevlError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        ir = None
        if args.against:
            try:
                ir = load_composition(args.against)
            except RevlError as error:
                print(f"error: cannot load composition {args.against}: {error}",
                      file=sys.stderr)
                return 1
        ok, reason = _attest.verify_attestation(att, key, ir)
        if args.json:
            print(json.dumps({"valid": ok, "reason": reason,
                              "composition_hash": att.get("composition_hash"),
                              "checked_composition": bool(ir)}, indent=2))
        else:
            print(_attest.render_verify(ok, reason, att))
        return 0 if ok else 1

    # sign mode
    try:
        ir = load_composition(args.target)
    except RevlError as error:
        print(f"error: cannot load composition {args.target}: {error}",
              file=sys.stderr)
        return 1
    import os  # noqa: PLC0415 — lazy: localized to this handler
    signer = args.signer or os.environ.get(_attest.SIGNER_ENV)
    try:
        att = _attest.make_attestation(ir, key, signer=signer)
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(att, indent=2))
    else:
        print(_attest.render_attestation(att))
    return 0


def _run_history_query(args) -> int:
    """`revl query {emitted-between,touched}` — the historical mode
    (docs/queries.md §9): the same query envelope answered against a RECORDED
    run rather than a static IR. Reads a replay-recording JSON and/or an
    item-27 lifecycle JSONL, never source."""
    from .. import query, why_runtime  # noqa: PLC0415

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


def _run_diff(args) -> int:
    """`revl diff BEFORE AFTER` — semantic composition diff (docs/revl-diff.md).

    Its own two-input loader (each side an IR/interchange doc or a source), so
    it is routed before the single shared compile step every other command
    uses."""
    from ..composition_diff import diff as composition_diff  # noqa: PLC0415
    from ..composition_diff import load_composition, render as render_diff

    try:
        before = load_composition(args.before)
        after = load_composition(args.after)
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    delta = composition_diff(before, after)
    if args.json:
        print(json.dumps(delta, indent=2))
    else:
        print(render_diff(delta, args.before, args.after))
    return 0


def _run_changelog(args) -> int:
    """`revl changelog --from OLD --to NEW` — the derived release note (item
    261). Two-input loader (each side an IR/interchange doc or a source), like
    `diff`, so it is routed before the single shared compile step. Always a
    render: exit 0, no acknowledgement model (the audit gate is the wall)."""
    from ..changelog import derive_changelog, render  # noqa: PLC0415
    from ..composition_diff import load_composition  # noqa: PLC0415

    try:
        before = load_composition(args.from_)
        after = load_composition(args.to)
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    doc = derive_changelog(
        before, after,
        previous_version=getattr(args, "current_version", None),
        no_semver=getattr(args, "no_semver", False),
        from_label=args.from_, to_label=args.to)
    # `--json` is the legacy alias; when it is set it forces JSON regardless of
    # `--format`, otherwise `--format` (default `markdown`) chooses the form.
    fmt = "json" if getattr(args, "json", False) else getattr(args, "format",
                                                              "markdown")
    print(render(doc, title=getattr(args, "title", None), fmt=fmt))
    return 0

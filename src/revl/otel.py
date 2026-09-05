"""OpenTelemetry export for revl's causal lifecycle traces (roadmap item 120).

:mod:`revl.why_runtime` records every runtime lifecycle transition as a
structured JSONL event whose ``cause`` names *why* the transition happened and
*who* caused it (see that module's vocabulary). Because the dependency graph is
a checked artifact (G2 unique provisions, G3 acyclic), the causal edges are
exact, not reconstructed from timing — which is precisely what a normal APM
cannot know. This module translates that vocabulary into the OpenTelemetry data
model so revl compositions show up, with their causality intact, in standard
observability stacks (Grafana / Datadog / Honeycomb / Jaeger).

This is a **format mapping**, not new semantics. The mapping, per vocabulary
element:

    lifecycle transition  (a ``load``/``withdraw`` event)  ->  a SPAN
    the transition's cause (the "why" recorded on it)       ->  a span EVENT
    a causal edge to the cause component (a provision)      ->  a span LINK
    the root cause         (``boot`` / ``trigger``)          ->  a child of the
                                                                synthetic run
                                                                root span

Parent/child nesting follows the cause chain exactly as
:meth:`why_runtime.Trace.cause_chain` walks it: a caused transition is a child
of the transition that caused it (a load's parent is its first provider's load;
a withdrawal's parent is the withdrawing provider's withdrawal). Links carry the
same edges so causality survives even a truncated trace where a parent span is
absent — the cause is *also* recorded as a span event on every span, so no
causal information is ever lost.

Design notes
------------

* **The OTel SDK is an optional dependency.** ``pip install revl[otel]`` pulls
  it in; the base install stays dependency-light. The mapping itself
  (:func:`build_spans`) is pure Python with no OTel import at all, so it is
  fully testable without the SDK — you assert the intermediate span structure.
  Only :func:`export_to_otel` touches the SDK, and if it is absent it returns a
  clear, structured no-op result instead of raising. There is never a hard
  ``import opentelemetry`` in any core path.

* **Off the hot path.** Nothing here runs during ``revl run``; it consumes an
  already-recorded trace post hoc. Turn it on by pointing it at a trace:
  ``python -P -m revl.otel run.jsonl`` (the `-P` is the PYTHONSAFEPATH
  safety bit: `-m` puts the CWD at `sys.path[0]`, and the otel subcommand
  imports ``opentelemetry`` from bare name so a sibling ``opentelemetry.py``
  in a composition's directory would shadow the real package; issue #317),
  or call :func:`export_trace_file` / :func:`export_to_otel` from your own
  code and pass any span exporter).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import why_runtime as wr

# --------------------------------------------------------------------------
# the intermediate span model — pure, no OTel SDK required
# --------------------------------------------------------------------------

ROOT_SPAN_ID = "run"

# span-status codes, mirroring OTel's StatusCode without importing it
STATUS_UNSET = "unset"
STATUS_OK = "ok"
STATUS_ERROR = "error"


@dataclass
class SpanEvent:
    """A point-in-time annotation on a span — here, the transition's cause
    (the "effect" recorded on the lifecycle move)."""

    name: str
    attributes: dict = field(default_factory=dict)


@dataclass
class SpanLink:
    """A typed reference from this span to the span of the component that
    caused the transition — a provision edge, preserved as OTel causality."""

    target: str  # the span_id this links to
    attributes: dict = field(default_factory=dict)


@dataclass
class SpanModel:
    """One OTel span, described independently of the SDK so the mapping can be
    built and asserted without ``opentelemetry`` installed."""

    span_id: str
    name: str
    kind: str  # "load" | "withdraw" | "run"
    parent_id: str | None
    component: str | None
    attributes: dict = field(default_factory=dict)
    status: str = STATUS_UNSET
    events: list[SpanEvent] = field(default_factory=list)
    links: list[SpanLink] = field(default_factory=list)


def _transition_ends(transition: str) -> tuple[str, str]:
    """Split ``"ACTIVE -> DISPOSED"`` into ``("ACTIVE", "DISPOSED")``.
    Tolerant of a malformed value (returns it as the target, empty source)."""
    parts = [p.strip() for p in str(transition).split("->", 1)]
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", parts[0]


def _status_for(event_kind: str, target_state: str) -> str:
    """A withdrawal into a FAILED state is an error span; everything else
    that completed is ok. (An unrecorded/`?` transition stays unset.)"""
    if not target_state or target_state == "?":
        return STATUS_UNSET
    if event_kind == wr.WITHDRAW and target_state == "FAILED":
        return STATUS_ERROR
    return STATUS_OK


def _span_id(seq) -> str:
    return f"s{seq}"


class _Index:
    """Resolves a (component, event-kind) reference to the span it names,
    the way ``why_runtime`` attributes causes: prefer a match in the same
    generation, and among those take the last recorded — mirroring
    ``Trace._event_matching``'s "last of the preferred kind"."""

    def __init__(self, events: list[dict]) -> None:
        # (component, event_kind) -> list of (seq, gen, span_id) in trace order
        self._by: dict[tuple[str, str], list[tuple]] = {}
        for ev in events:
            key = (ev.get("component"), ev.get("event"))
            self._by.setdefault(key, []).append(
                (ev.get("seq"), ev.get("gen"), _span_id(ev.get("seq"))))

    def resolve(self, component: str | None, event_kind: str,
                gen) -> str | None:
        if component is None:
            return None
        candidates = self._by.get((component, event_kind))
        if not candidates:
            return None
        same_gen = [c for c in candidates if c[1] == gen]
        pool = same_gen or candidates
        return pool[-1][2]


def _cause_event(cause: dict) -> SpanEvent:
    """The transition's cause, as a span event. Name is ``cause:<kind>``;
    attributes flatten the cause dict into OTel-friendly scalars, and
    ``revl.cause.note`` carries ``why_runtime``'s own human phrasing so the
    "why" reads identically in a trace UI and in ``revl why``."""
    kind = cause.get("kind", "unknown")
    attrs: dict = {"revl.cause.kind": kind,
                   "revl.cause.note": wr._cause_note(cause)}
    # v2: a FAILED settle attaches its diagnostic code to the cause (trigger or
    # provider-withdrawn). Surface it when present; absent on v1 and on an
    # unclassifiable failure, so no span ever carries a fabricated code.
    if cause.get("code") is not None:
        attrs["revl.cause.code"] = cause.get("code")
    if kind == wr.TRIGGER:
        attrs["revl.cause.detail"] = cause.get("detail") or ""
    elif kind == wr.PROVIDER_WITHDRAWN:
        attrs["revl.cause.provider"] = cause.get("component") or ""
        attrs["revl.cause.key"] = cause.get("key") or ""
    elif kind == wr.REQUIREMENTS:
        providers = cause.get("providers") or []
        attrs["revl.cause.providers"] = [
            f"{p.get('component')}:{p.get('key')}" for p in providers]
    return SpanEvent(name=f"cause:{kind}", attributes=attrs)


def _links_and_parent(cause: dict, gen, index: _Index) -> tuple[list[SpanLink], str | None]:
    """The causal edges of a transition as span links, plus the parent span.

    * ``requirements`` — links to each provider's *load* span; the parent is
      the first provider's load span (the head of ``cause_chain``'s walk). No
      providers (a boot-equivalent root load) -> parented on the run root.
    * ``provider-withdrawn`` — a link to, and parented on, the provider's
      *withdraw* span.
    * ``boot`` / ``trigger`` — a root cause: no links, parented on the run
      root.

    A referenced span that isn't in the trace (a truncated recording) yields
    no link and falls back to the run root for the parent — the cause is still
    on the span as an event, so the "why" is never lost."""
    kind = cause.get("kind")
    links: list[SpanLink] = []

    if kind == wr.REQUIREMENTS:
        providers = cause.get("providers") or []
        parent = None
        for i, prov in enumerate(providers):
            target = index.resolve(prov.get("component"), wr.LOAD, gen)
            if target is not None:
                links.append(SpanLink(target=target, attributes={
                    "revl.link.relation": "requirement",
                    "revl.link.provider": prov.get("component") or "",
                    "revl.link.key": prov.get("key") or "",
                }))
                if i == 0:
                    parent = target
        return links, (parent or ROOT_SPAN_ID)

    if kind == wr.PROVIDER_WITHDRAWN:
        target = index.resolve(cause.get("component"), wr.WITHDRAW, gen)
        if target is not None:
            links.append(SpanLink(target=target, attributes={
                "revl.link.relation": "provider-withdrawn",
                "revl.link.provider": cause.get("component") or "",
                "revl.link.key": cause.get("key") or "",
            }))
            return links, target
        return links, ROOT_SPAN_ID

    # boot / trigger / anything else — a root cause under the run root
    return links, ROOT_SPAN_ID


def build_spans(events: list[dict], *, run_name: str = "revl run") -> list[SpanModel]:
    """Map a list of ``why_runtime`` trace events to :class:`SpanModel`s.

    Pure and SDK-free: this is the whole mapping, and the thing to assert in
    tests that must run without ``opentelemetry`` installed. Returns the
    synthetic run-root span first, then one span per trace event in trace
    order. Non-events / malformed records are skipped defensively."""
    valid = [e for e in events if isinstance(e, dict) and e.get("event")]
    index = _Index(valid)

    root = SpanModel(
        span_id=ROOT_SPAN_ID, name=run_name, kind="run", parent_id=None,
        component=None, status=STATUS_OK,
        attributes={"revl.trace.events": len(valid),
                    "revl.schema.version": wr.SCHEMA_VERSION})
    spans: list[SpanModel] = [root]

    for ev in valid:
        event_kind = ev.get("event")
        component = ev.get("component")
        transition = ev.get("transition", "?")
        gen = ev.get("gen")
        cause = ev.get("cause") or {}
        src, dst = _transition_ends(transition)
        links, parent = _links_and_parent(cause, gen, index)

        attributes = {
            "revl.seq": ev.get("seq"),
            "revl.gen": gen,
            "revl.event": event_kind,
            "revl.component": component,
            "revl.transition": transition,
            "revl.transition.from": src,
            "revl.transition.to": dst,
            "revl.cause.kind": cause.get("kind", "unknown"),
        }
        # v2 additions, surfaced when present so a v2 trace loses nothing and a
        # v1 trace (no ts, no emit, no code) is unaffected. An `emit` event has
        # no transition and carries a capability + key instead; it maps to a
        # plain span (unset status) so the mapping never regresses on it.
        if ev.get("ts") is not None:
            attributes["revl.ts"] = ev.get("ts")
        if ev.get("activationId") is not None:
            attributes["revl.activation.id"] = ev.get("activationId")
        if event_kind == wr.EMIT:
            attributes["revl.capability"] = ev.get("capability") or ""
            attributes["revl.emission.key"] = ev.get("key") or ""
            # item 121: a model completion crossing carries an `llm` payload;
            # flatten it onto the span as OTel-scalar attributes so it lights up
            # as a model call, not an opaque emission (§3.1). Additive: a
            # non-model emit has no `llm` and is mapped exactly as before.
            links = links + _llm_attributes(ev.get("llm"), attributes)

        spans.append(SpanModel(
            span_id=_span_id(ev.get("seq")),
            name=f"{component} {event_kind}",
            kind=event_kind,
            parent_id=parent,
            component=component,
            status=_status_for(event_kind, dst),
            attributes=attributes,
            events=[_cause_event(cause)],
            links=links,
        ))
    return spans


def _llm_attributes(llm: dict | None, attributes: dict) -> list[SpanLink]:
    """Flatten an `emit` event's item-121 `llm` payload onto the span
    (`attributes`, in place) and return any `model-produced` span links.

    GenAI semantic-convention names are used where they exist so the span reads
    as a model call in Grafana/Datadog/Honeycomb; provenance rides onto the span
    so a downstream dashboard cannot silently promote a host number to ground
    truth (§3.1). Two invariants hold the safety line:

    * NO prompt/response text ever becomes a span attribute (§3.1 rule 2). Only
      the salted digest may ride, as ``revl.llm.prompt.sha256``, never the text;
      a suppressed digest is simply absent (`build_spans` copies only present
      fields, `otel.py` discipline).
    * The `produced` edge is a `SpanLink` ONLY when `producedSeq` is present (the
      emitter's static value-flow fact and the fiber-local token together proved
      it, §3.1 rule 1). When absent it is NEVER
      adjacency-guessed and NEVER exported as a hard proven cause — a wrong
      SpanLink shipped to a third-party backend reads as proof (§4 attack 3)."""
    if not isinstance(llm, dict):
        return []
    if llm.get("model") is not None:
        attributes["gen_ai.request.model"] = llm.get("model")
    if llm.get("tokensIn") is not None:
        attributes["gen_ai.usage.input_tokens"] = llm.get("tokensIn")
    if llm.get("tokensOut") is not None:
        attributes["gen_ai.usage.output_tokens"] = llm.get("tokensOut")
    cost = llm.get("cost")
    if isinstance(cost, dict):
        attributes["revl.llm.cost"] = cost.get("amount")
        attributes["revl.llm.cost.currency"] = cost.get("currency")
        attributes["revl.llm.cost.provenance"] = cost.get("provenance") or "host-reported"
    if llm.get("latencySeconds") is not None:
        attributes["revl.llm.latency"] = llm.get("latencySeconds")
        attributes["revl.llm.latency.provenance"] = \
            llm.get("latencyProvenance") or "revl-measured-bracket"
    if llm.get("attempts") is not None:
        attributes["revl.llm.attempts"] = llm.get("attempts")
    if llm.get("attemptCeiling") is not None:
        attributes["revl.llm.attempt_ceiling"] = llm.get("attemptCeiling")
    attributes["revl.llm.attempts.provenance"] = \
        llm.get("attemptsProvenance") or "revl-controlled"
    if llm.get("model") is not None:
        attributes["revl.llm.model.provenance"] = \
            llm.get("modelProvenance") or "host-reported"
    if llm.get("tokensIn") is not None or llm.get("tokensOut") is not None:
        attributes["revl.llm.usage.provenance"] = \
            llm.get("usageProvenance") or "host-reported"
    if llm.get("verifiedBy"):
        attributes["revl.llm.verified_by"] = list(llm.get("verifiedBy"))
    digest = llm.get("promptDigest")
    if isinstance(digest, dict):
        # the salted HMAC and coarse bucket only — never any prompt/response text
        attributes["revl.llm.prompt.sha256"] = digest.get("salted")
        attributes["revl.llm.prompt.bytes_bucket"] = digest.get("bytesBucket")

    links: list[SpanLink] = []
    for target_seq in llm.get("producedSeq") or []:
        links.append(SpanLink(target=_span_id(target_seq), attributes={
            "revl.link.relation": "model-produced"}))
    return links


# --------------------------------------------------------------------------
# the OTel SDK path — optional, degrades gracefully when absent
# --------------------------------------------------------------------------


def otel_available() -> bool:
    """True when the OpenTelemetry SDK is importable (``revl[otel]``)."""
    try:  # pragma: no cover — trivial, and gated by whether the SDK is present
        import opentelemetry.sdk.trace  # noqa: F401
        import opentelemetry.trace  # noqa: F401
        return True
    except ImportError:
        return False



def _topo_order(spans: list[SpanModel]) -> list[SpanModel]:
    """Parents before children. The trace is a tree (each span has one
    parent), but a withdrawal's parent (its provider's withdrawal) can appear
    *later* in trace order, so a plain trace-order pass won't do."""
    by_id = {s.span_id: s for s in spans}
    ordered: list[SpanModel] = []
    done: set[str] = set()

    def visit(span: SpanModel) -> None:
        if span.span_id in done:
            return
        parent = by_id.get(span.parent_id) if span.parent_id else None
        if parent is not None and parent.span_id not in done:
            visit(parent)
        done.add(span.span_id)
        ordered.append(span)

    for s in spans:
        visit(s)
    return ordered


def export_to_otel(events: list[dict], *, span_exporter=None,
                   tracer_provider=None, run_name: str = "revl run") -> dict:
    """Emit a trace's spans through the OpenTelemetry SDK.

    Builds the mapping with :func:`build_spans`, then materialises each
    :class:`SpanModel` as a real OTel span — parent context, links, cause
    events and status — under one shared trace id.

    ``span_exporter`` — any ``opentelemetry.sdk.trace.export.SpanExporter``
    (Console, OTLP, …). Defaults to ``ConsoleSpanExporter``. Ignored when a
    ``tracer_provider`` is supplied (you own its processors then).

    Returns a structured result dict. When the SDK is absent this is a clean
    no-op: ``{"exported": False, "reason": ...}`` — never an ImportError."""
    if not otel_available():
        return {
            "exported": False,
            "reason": "the OpenTelemetry SDK is not installed; "
                      "run `pip install revl[otel]` to enable OTel export",
            "spans": len(build_spans(events, run_name=run_name)),
        }

    # imports are local so the module never hard-depends on the SDK
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )
    from opentelemetry.trace import (
        Link,
        NonRecordingSpan,
        SpanContext,
        Status,
        StatusCode,
        set_span_in_context,
    )

    owns_provider = tracer_provider is None
    if owns_provider:
        tracer_provider = TracerProvider(
            resource=Resource.create({"service.name": "revl"}))
        exporter = span_exporter or ConsoleSpanExporter()
        tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))

    tracer = tracer_provider.get_tracer("revl.otel")

    status_map = {
        STATUS_OK: StatusCode.OK,
        STATUS_ERROR: StatusCode.ERROR,
        STATUS_UNSET: StatusCode.UNSET,
    }

    spans = build_spans(events, run_name=run_name)
    contexts: dict[str, SpanContext] = {}
    exported = 0
    for model in _topo_order(spans):
        # OTel Links reference already-materialised spans; parents precede
        # children in topo order and links point at parents/earlier loads, so
        # any resolvable target is present. Unresolved targets are skipped.
        links = []
        for link in model.links:
            ctx = contexts.get(link.target)
            if ctx is not None:
                links.append(Link(ctx, attributes=dict(link.attributes)))

        parent_ctx = contexts.get(model.parent_id) if model.parent_id else None
        parent_context = (
            set_span_in_context(NonRecordingSpan(parent_ctx))
            if parent_ctx is not None else None)

        # scalar-clean attributes (drop Nones; OTel rejects them)
        attributes = {k: v for k, v in model.attributes.items() if v is not None}

        span = tracer.start_span(
            model.name, context=parent_context, links=links,
            attributes=attributes)

        # record this span's context so its children and links resolve to it
        contexts[model.span_id] = span.get_span_context()

        for sev in model.events:
            span.add_event(sev.name, attributes={
                k: v for k, v in sev.attributes.items() if v is not None})
        span.set_status(Status(status_map.get(model.status, StatusCode.UNSET)))
        span.end()
        exported += 1

    if owns_provider:
        tracer_provider.force_flush()
        tracer_provider.shutdown()

    return {"exported": True, "spans": exported}


def export_trace_file(path: str, **kwargs) -> dict:
    """Read a JSONL trace and export it. Convenience over
    :func:`export_to_otel`; accepts the same keyword arguments."""
    return export_to_otel(wr.read_trace(path), **kwargs)


# --------------------------------------------------------------------------
# opt-in CLI: `python -P -m revl.otel <trace.jsonl>` (the `-P` is the
# PYTHONSAFEPATH safety bit, issue #317)
# --------------------------------------------------------------------------


def _model_to_dict(span: SpanModel) -> dict:
    return {
        "span_id": span.span_id,
        "name": span.name,
        "kind": span.kind,
        "parent_id": span.parent_id,
        "component": span.component,
        "status": span.status,
        "attributes": span.attributes,
        "events": [{"name": e.name, "attributes": e.attributes}
                   for e in span.events],
        "links": [{"target": l.target, "attributes": l.attributes}
                  for l in span.links],
    }


def main(argv: list[str] | None = None) -> int:
    """Export a recorded trace to OpenTelemetry, or (with ``--json``, or when
    the SDK is absent) print the intermediate span mapping so the translation
    is inspectable everywhere."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -P -m revl.otel",  # PYTHONSAFEPATH safety bit (issue #317)
        description="Export a revl causal lifecycle trace to OpenTelemetry.")
    parser.add_argument("trace", help="path to a why_runtime JSONL trace")
    parser.add_argument("--json", action="store_true",
                        help="print the intermediate span mapping as JSON "
                             "instead of emitting through the OTel SDK")
    parser.add_argument("--run-name", default="revl run",
                        help="name for the synthetic run-root span")
    args = parser.parse_args(argv)

    events = wr.read_trace(args.trace)

    if args.json or not otel_available():
        spans = build_spans(events, run_name=args.run_name)
        print(json.dumps([_model_to_dict(s) for s in spans], indent=2))
        if not args.json:
            print("note: the OpenTelemetry SDK is not installed — printed the "
                  "span mapping instead. `pip install revl[otel]` to emit "
                  "spans to a collector.", file=sys.stderr)
        return 0

    result = export_to_otel(events, run_name=args.run_name)
    if not result.get("exported"):
        print(result.get("reason", "export failed"), file=sys.stderr)
        return 1
    print(f"exported {result['spans']} spans to OpenTelemetry", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

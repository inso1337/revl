# OpenTelemetry export (`revl.otel`)

`revl run` records every lifecycle transition of a composition as a structured
JSONL trace, where each transition carries its **cause** — who caused it and why
(see [`why-runtime`](why-runtime.md) and `src/revl/why_runtime.py`). Because
revl's dependency graph is a *checked* artifact (G2 makes each provision unique,
G3 makes the graph acyclic), those causal edges are exact, not reconstructed
from timing — which is exactly what a conventional APM cannot know.

`revl.otel` translates that causal vocabulary into the OpenTelemetry data model,
so a revl composition shows up — with its causality intact — in any standard
observability stack (Grafana Tempo, Datadog, Honeycomb, Jaeger).

This is a **format mapping**, not new semantics. The traces are already
structured and causal; we translate them.

## The mapping

| `why_runtime` vocabulary | OpenTelemetry |
| --- | --- |
| a lifecycle transition — a `load` or `withdraw` event | a **span** (`"<Component> load"` / `"<Component> withdraw"`) |
| the transition's state move, e.g. `ACTIVE -> DISPOSED` | span attributes `revl.transition.from` / `.to` (and the full `revl.transition`) |
| the transition's **cause** (the "why" recorded on it) | a **span event** named `cause:<kind>` |
| a `requirements` edge — a load's resolved providers | a **span link** per provider (relation `requirement`) to that provider's *load* span |
| a `provider-withdrawn` edge — a teardown's cause | a **span link** (relation `provider-withdrawn`) to the provider's *withdraw* span |
| the root cause — `boot` or `trigger` | a child of the synthetic **run root** span |

### Parent/child follows the cause chain

A caused transition is the **child** of the transition that caused it, exactly
as `why_runtime.Trace.cause_chain` walks it:

- a `requirements` load is parented on its **first provider's load** span (and
  links to every provider);
- a `provider-withdrawn` teardown is parented on the **provider's withdraw**
  span — even though, under LIFO teardown, that provider withdrawal is recorded
  *later* in the trace than the dependent's; the mapping resolves it by
  component and generation regardless of trace order;
- `boot` and `trigger` roots hang directly off the synthetic run root, so one
  export is one connected trace.

Every span also carries its cause as a span **event**, so even a *truncated*
trace (where a referenced span was never recorded) keeps the "why": the link is
dropped and the parent falls back to the run root, but no causal information is
lost.

### Status

A `withdraw` into a `FAILED` state becomes an **error** span; every other
completed transition is `OK`.

## Enabling it

The OpenTelemetry SDK is an **optional dependency** — the base `revl` install
stays dependency-light and imports no OTel code on any core path.

```bash
pip install revl[otel]
```

Point the exporter at a recorded trace:

```bash
# emit spans through the OpenTelemetry SDK (default: console exporter)
python -m revl.otel run.jsonl

# print the intermediate span mapping as JSON instead (works with or without
# the SDK installed — useful for inspecting the translation)
python -m revl.otel run.jsonl --json
```

If the SDK is **not** installed, `python -m revl.otel run.jsonl` degrades
gracefully: it prints the span mapping as JSON and a note telling you to install
`revl[otel]` — never a hard `ImportError`.

> Coordination note (item 123 owns `__main__.py`): the opt-in is deliberately a
> standalone `python -m revl.otel` entry point rather than a `revl … --otel`
> flag, so this change touches neither the CLI dispatcher nor the runtime hot
> path. If a `revl`-subcommand `--otel` flag is wanted later, it can call
> `revl.otel.export_trace_file` — the integration surface is already a single
> function.

## From your own code

```python
from revl import otel

# any opentelemetry SpanExporter (OTLP, Console, in-memory, …)
otel.export_trace_file("run.jsonl", span_exporter=my_exporter)

# or bring your own configured TracerProvider (you own its processors then)
otel.export_to_otel(events, tracer_provider=my_provider)
```

Both return a structured result: `{"exported": True, "spans": N}` on success,
or `{"exported": False, "reason": ...}` when the SDK is absent.

### The mapping is testable without the SDK

`revl.otel.build_spans(events)` is pure Python with no OTel import. It returns
the intermediate `SpanModel` list — span id, name, kind, parent, attributes,
status, `events` (causes) and `links` (provision edges) — which is what the
mapping tests assert directly. The SDK is only touched by `export_to_otel`.
```

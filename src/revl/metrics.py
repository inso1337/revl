"""Capability-aware runtime metrics over a recorded lifecycle trace.

`revl metrics <run.jsonl>` is the aggregate companion to `revl why` (one
component's cause chain) and the OTel export (every transition as a span). Where
those explain or forward *individual* events, this rolls a whole run up into
three numbers a supervisor actually watches:

1. **emission count by capability** — how many irreversible emissions crossed a
   boundary, bucketed by the service capability they were scoped to (and, as a
   sub-breakdown, by the emission label). This is the runtime counterpart to
   `revl audit`'s *static* capability surface: audit says which boundaries a
   component *may* reach; metrics counts which it *did*.
2. **failure count by G-rule** — how many `withdraw`s settled a fiber into
   `FAILED`, bucketed by the diagnostic code the failure classified as
   (`diagnostics.classify`, e.g. `A8`); a failure that carried no classifiable
   `RevlError` (a bare crash, no `code`) buckets as `"unclassified"` rather than
   under a fabricated code.
3. **average lifecycle duration** — per component, the mean of
   ``withdraw.ts − load.ts`` over each component's load→withdraw lifecycles
   (paired by component **and** generation), plus the lifecycle count.

This module is pure — it imports no runtime and reads only the JSONL trace
vocabulary from :mod:`revl.why_runtime`. `compute_metrics(events)` is the whole
computation (unit-testable on hand-written events); `render(metrics)` is the
human table; `metrics_from_file(path)` is the file loader. The CLI (`revl
metrics`, and `--json`) always exits 0 — reporting a run's shape is never a
gate.

### Schema v1 graceful degradation (detected by data, not by `v`)

The trace schema is v2 (docs/why-runtime.md); the fields these metrics need
(`ts` on every event, `code` on a FAILED cause, the `emit` event kind) all
arrived additively. A v1 trace, or a v2 trace an older recorder wrote without
them, simply *lacks* the input:

* **duration** needs `ts`. When load/withdraw lifecycles are present but none
  carry `ts`, the metric is reported as ``{"unavailable": …}`` — detected by the
  *absence of `ts` on the events being paired*, never by reading `v` alone (a
  recorder is free to stamp some events and not others).
* **emissions** needs `emit` events; a trace with none yields a total of 0.
* **failures** needs `code` on the FAILED cause; without it every failure
  buckets as `"unclassified"` — the honest degrade, not a guess.
"""

from __future__ import annotations


from .why_runtime import EMIT, LOAD, WITHDRAW, read_trace

# the bucket a FAILED withdraw with no classifiable code lands in
UNCLASSIFIED = "unclassified"

# the state a withdraw settles into that counts as a failure
_FAILED = "FAILED"

_TS_UNAVAILABLE = "ts not present (trace schema v1)"


def _is_failed(event: dict) -> bool:
    """A withdraw that settled a fiber into FAILED. Read from the observed
    `transition` (``"ACTIVE -> FAILED"``), which is what actually happened —
    the same split why_runtime keeps between the observed transition and the
    graph-derived cause."""
    if event.get("event") != WITHDRAW:
        return False
    transition = event.get("transition") or ""
    # the settle end of "<from> -> <to>"; robust to a missing/odd transition
    end = transition.split("->")[-1].strip() if "->" in transition else transition.strip()
    return end == _FAILED


def _emission_metrics(events: list[dict]) -> dict:
    """Metric 1: emission crossings bucketed by capability, with a by-key
    sub-breakdown. A v1 trace carries no `emit` events, so this is naturally a
    total of 0 — no `emit`, no crossing."""
    by_capability: dict[str, int] = {}
    by_key: dict[str, int] = {}
    total = 0
    for event in events:
        if event.get("event") != EMIT:
            continue
        total += 1
        capability = event.get("capability")
        # a well-formed emit always names its capability; degrade a malformed
        # one to the unclassified bucket rather than crashing on None
        cap_bucket = capability if capability is not None else UNCLASSIFIED
        by_capability[cap_bucket] = by_capability.get(cap_bucket, 0) + 1
        key = event.get("key")
        if key is not None:
            by_key[key] = by_key.get(key, 0) + 1
    return {
        "total": total,
        "by_capability": dict(sorted(by_capability.items())),
        "by_key": dict(sorted(by_key.items())),
    }


def _failure_metrics(events: list[dict]) -> dict:
    """Metric 2: FAILED withdraws bucketed by the diagnostic code on their
    cause; an absent code (a bare crash) buckets as ``"unclassified"``. A v1
    trace never carries `code`, so every failure it records buckets as
    unclassified — the honest degrade."""
    by_code: dict[str, int] = {}
    total = 0
    for event in events:
        if not _is_failed(event):
            continue
        total += 1
        code = (event.get("cause") or {}).get("code")
        bucket = code if code is not None else UNCLASSIFIED
        by_code[bucket] = by_code.get(bucket, 0) + 1
    return {
        "total": total,
        "by_code": dict(sorted(by_code.items())),
    }


def _lifecycles(events: list[dict]) -> dict[tuple, dict]:
    """Group load/withdraw events into lifecycles keyed by
    ``(component, gen)`` — the pairing the trace makes unambiguous: a component
    loads once and withdraws once per generation, so one key names exactly one
    lifecycle. The first load and first withdraw seen for a key are taken (a
    trace should carry one of each; taking the first is robust to a duplicate)."""
    lifecycles: dict[tuple, dict] = {}
    for event in events:
        kind = event.get("event")
        if kind not in (LOAD, WITHDRAW):
            continue
        key = (event.get("component"), event.get("gen"))
        slot = lifecycles.setdefault(key, {"load": None, "withdraw": None})
        if kind == LOAD and slot["load"] is None:
            slot["load"] = event
        elif kind == WITHDRAW and slot["withdraw"] is None:
            slot["withdraw"] = event
    return lifecycles


def _duration_metrics(events: list[dict]) -> dict:
    """Metric 3: average ``withdraw.ts − load.ts`` per component, over each
    component's completed lifecycles (both a load and a withdraw, paired by
    component+gen), and the lifecycle count.

    Graceful degrade (by data, not by `v`): if lifecycles exist to measure but
    none of the paired events carry `ts`, the metric is unavailable. An
    unmatched load (still active) or unmatched withdraw (no recorded load)
    contributes no duration and is simply not counted."""
    lifecycles = _lifecycles(events)
    per_component: dict[str, list[float]] = {}
    saw_pair_without_ts = False
    for (component, _gen), slot in lifecycles.items():
        load, withdraw = slot["load"], slot["withdraw"]
        if load is None or withdraw is None:
            # an incomplete lifecycle — never active-and-gone, so no duration
            continue
        load_ts, withdraw_ts = load.get("ts"), withdraw.get("ts")
        if load_ts is None or withdraw_ts is None:
            saw_pair_without_ts = True
            continue
        per_component.setdefault(component, []).append(withdraw_ts - load_ts)

    measured = sum(len(v) for v in per_component.values())
    if measured == 0:
        if saw_pair_without_ts:
            # completed lifecycles exist but carry no ts — the v1 degrade,
            # detected via missing ts rather than the schema version field
            return {"unavailable": _TS_UNAVAILABLE}
        # genuinely nothing to measure (no completed lifecycle at all)
        return {"count": 0, "avg_seconds": None, "by_component": {}}

    by_component = {
        name: {"count": len(durs), "avg_seconds": sum(durs) / len(durs)}
        for name, durs in sorted(per_component.items())
    }
    all_durs = [d for durs in per_component.values() for d in durs]
    return {
        "count": len(all_durs),
        "avg_seconds": sum(all_durs) / len(all_durs),
        "by_component": by_component,
    }


def compute_metrics(events: list[dict]) -> dict:
    """The whole computation, pure over a list of trace events (the
    :mod:`revl.why_runtime` vocabulary). Returns a machine-readable metrics
    document — the shape `revl metrics --json` prints and `render` formats."""
    return {
        "events": len(events),
        "emissions": _emission_metrics(events),
        "failures": _failure_metrics(events),
        "lifecycleDuration": _duration_metrics(events),
    }


def metrics_from_file(path: str) -> dict:
    """Read a JSONL trace from `path` and compute its metrics."""
    return compute_metrics(read_trace(path))


# --------------------------------------------------------------------------
# human rendering
# --------------------------------------------------------------------------


def _fmt_seconds(value: float) -> str:
    return f"{value:.6f}s"


def render(metrics: dict) -> str:
    """A compact human table of the three metrics (the default CLI output;
    `--json` prints the full document `compute_metrics` returns)."""
    lines = [f"metrics over {metrics.get('events', 0)} trace event(s)"]

    # 1. emissions by capability
    emissions = metrics.get("emissions") or {}
    lines.append("")
    lines.append(f"emissions by capability (total {emissions.get('total', 0)}):")
    by_capability = emissions.get("by_capability") or {}
    if not by_capability:
        lines.append("  (none crossed)")
    else:
        width = max(len(c) for c in by_capability)
        for cap, count in by_capability.items():
            lines.append(f"  {cap.ljust(width)}  {count}")
        by_key = emissions.get("by_key") or {}
        if by_key:
            lines.append("  by key:")
            kwidth = max(len(k) for k in by_key)
            for key, count in by_key.items():
                lines.append(f"    {key.ljust(kwidth)}  {count}")

    # 2. failures by G-rule
    failures = metrics.get("failures") or {}
    lines.append("")
    lines.append(f"failures by G-rule (total {failures.get('total', 0)}):")
    by_code = failures.get("by_code") or {}
    if not by_code:
        lines.append("  (no FAILED withdrawals)")
    else:
        width = max(len(c) for c in by_code)
        for code, count in by_code.items():
            lines.append(f"  {code.ljust(width)}  {count}")

    # 3. avg lifecycle duration
    duration = metrics.get("lifecycleDuration") or {}
    lines.append("")
    if "unavailable" in duration:
        lines.append(f"avg lifecycle duration: unavailable — {duration['unavailable']}")
    else:
        count = duration.get("count", 0)
        avg = duration.get("avg_seconds")
        if count == 0 or avg is None:
            lines.append("avg lifecycle duration: (no completed lifecycles)")
        else:
            lines.append(f"avg lifecycle duration: {_fmt_seconds(avg)} "
                         f"over {count} lifecycle(s)")
            by_component = duration.get("by_component") or {}
            width = max(len(c) for c in by_component)
            for name, stats in by_component.items():
                lines.append(f"  {name.ljust(width)}  "
                             f"{_fmt_seconds(stats['avg_seconds'])} "
                             f"(x{stats['count']})")
    return "\n".join(lines)

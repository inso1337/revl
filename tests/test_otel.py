"""OpenTelemetry export of causal lifecycle traces (roadmap item 120,
docs/opentelemetry.md).

Two layers, mirroring test_why_runtime.py's honesty split:

* the **mapping** layer is pure — :func:`revl.otel.build_spans` translates the
  `why_runtime` trace vocabulary to an intermediate span model with no
  ``opentelemetry`` import at all, so these tests run on every interpreter and
  assert the translation directly (lifecycle->span, cause->span event,
  provision edge->span link, causality preserved);
* the **SDK** layer actually feeds those spans through the OpenTelemetry SDK
  and captures them via an in-memory exporter; it `skipif`s cleanly when the
  optional dep is absent, and a matching test asserts the *absent* path is a
  structured no-op rather than an ImportError.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import otel  # noqa: E402
from revl import why_runtime as wr  # noqa: E402


# the same hand-built cascade test_why_runtime.py uses: PgDatabase booted,
# UserCache loaded requiring `db`, then PgDatabase withdrawn by trigger and
# UserCache torn down because its provider withdrew.
def _cascade_trace() -> list[dict]:
    return [
        wr.make_event(0, 1, wr.LOAD, "PgDatabase", "PENDING -> ACTIVE",
                      wr.cause_boot()),
        wr.make_event(1, 1, wr.LOAD, "UserCache", "PENDING -> ACTIVE",
                      wr.cause_requirements(
                          [{"component": "PgDatabase", "key": "db"}])),
        wr.make_event(2, 1, wr.WITHDRAW, "UserCache", "ACTIVE -> PENDING",
                      wr.cause_provider_withdrawn("PgDatabase", "db")),
        wr.make_event(3, 1, wr.WITHDRAW, "PgDatabase", "ACTIVE -> DISPOSED",
                      wr.cause_trigger("withdrawn by operator")),
    ]


def _by_id(spans):
    return {s.span_id: s for s in spans}


# ---- the pure mapping (no OTel SDK needed) -------------------------------


def test_every_transition_becomes_a_span_under_the_run_root():
    spans = otel.build_spans(_cascade_trace())
    # one synthetic run root + one span per trace event
    assert spans[0].span_id == otel.ROOT_SPAN_ID
    assert spans[0].kind == "run"
    assert len([s for s in spans if s.kind != "run"]) == 4
    # lifecycle kind carried through
    kinds = {s.component: s.kind for s in spans if s.component}
    assert kinds == {"PgDatabase": wr.WITHDRAW, "UserCache": wr.WITHDRAW}  # last per component
    # transition split into from/to attributes
    load = next(s for s in spans if s.name == "PgDatabase load")
    assert load.attributes["revl.transition.from"] == "PENDING"
    assert load.attributes["revl.transition.to"] == "ACTIVE"


def test_cause_is_recorded_as_a_span_event():
    spans = _by_id(otel.build_spans(_cascade_trace()))
    # the trigger withdrawal: PgDatabase seq 3
    trigger = spans["s3"]
    assert trigger.events[0].name == f"cause:{wr.TRIGGER}"
    assert trigger.events[0].attributes["revl.cause.detail"] == "withdrawn by operator"
    # the note mirrors why_runtime's own human phrasing
    assert trigger.events[0].attributes["revl.cause.note"] == "withdrawn by operator"
    # the requirements load carries its providers on the event
    load = spans["s1"]
    assert load.events[0].name == f"cause:{wr.REQUIREMENTS}"
    assert load.events[0].attributes["revl.cause.providers"] == ["PgDatabase:db"]


def test_provision_edges_become_links_and_parents():
    spans = _by_id(otel.build_spans(_cascade_trace()))

    # requirements: UserCache load (s1) links to and is parented on
    # PgDatabase's load (s0)
    uc_load = spans["s1"]
    assert uc_load.parent_id == "s0"
    assert [l.target for l in uc_load.links] == ["s0"]
    assert uc_load.links[0].attributes["revl.link.relation"] == "requirement"

    # provider-withdrawn: UserCache withdraw (s2) links to and is parented on
    # PgDatabase's withdraw (s3) — even though that provider withdrawal is
    # recorded *later* in the trace (LIFO), the mapping resolves it.
    uc_withdraw = spans["s2"]
    assert uc_withdraw.parent_id == "s3"
    assert [l.target for l in uc_withdraw.links] == ["s3"]
    assert uc_withdraw.links[0].attributes["revl.link.relation"] == "provider-withdrawn"

    # roots (boot load, trigger withdraw) hang off the run root, no links
    assert spans["s0"].parent_id == otel.ROOT_SPAN_ID  # boot
    assert spans["s0"].links == []
    assert spans["s3"].parent_id == otel.ROOT_SPAN_ID  # trigger
    assert spans["s3"].links == []


def test_parent_chain_matches_why_runtime_cause_chain():
    """The span parent walk must reproduce Trace.cause_chain's component order
    — the mapping isn't allowed to invent a different causality."""
    trace = wr.Trace(_cascade_trace())
    spans = _by_id(otel.build_spans(_cascade_trace()))

    # follow span parents up from UserCache's withdrawal, collecting components
    walk = []
    cur = spans["s2"]
    while cur is not None and cur.span_id != otel.ROOT_SPAN_ID:
        walk.append(cur.component)
        cur = spans.get(cur.parent_id)
    assert walk == [f.component for f in trace.cause_chain("UserCache")]


def test_failed_withdrawal_is_an_error_span():
    events = [
        wr.make_event(0, 1, wr.LOAD, "Svc", "PENDING -> ACTIVE", wr.cause_boot()),
        wr.make_event(1, 1, wr.WITHDRAW, "Svc", "ACTIVE -> FAILED",
                      wr.cause_trigger("crashed")),
    ]
    spans = _by_id(otel.build_spans(events))
    assert spans["s1"].status == otel.STATUS_ERROR
    assert spans["s0"].status == otel.STATUS_OK


def test_truncated_trace_keeps_the_cause_but_drops_the_missing_link():
    """A withdrawal whose provider withdrawal was never recorded still gets its
    cause as a span event; it just has no link and falls back to the run root,
    so no causal information is silently lost."""
    events = [
        wr.make_event(0, 1, wr.WITHDRAW, "UserCache", "ACTIVE -> PENDING",
                      wr.cause_provider_withdrawn("PgDatabase", "db")),
    ]
    spans = _by_id(otel.build_spans(events))
    uc = spans["s0"]
    assert uc.links == []                      # target span absent
    assert uc.parent_id == otel.ROOT_SPAN_ID   # graceful fallback
    assert uc.events[0].name == f"cause:{wr.PROVIDER_WITHDRAWN}"  # cause kept


def test_malformed_records_are_skipped():
    events = [{"not": "an event"}, wr.make_event(0, 1, wr.LOAD, "A",
              "PENDING -> ACTIVE", wr.cause_boot())]
    spans = otel.build_spans(events)
    assert [s.component for s in spans if s.component] == ["A"]


# ---- schema v2 tolerance: ts, failure code, emit events ------------------


def _v2_trace_with_emit() -> list[dict]:
    """The conforming cascade, plus a v2 `emit` event (an emission crossing) and
    a `ts` on the transitions — everything a v2 trace can carry."""
    return [
        wr.make_event(0, 1, wr.LOAD, "PgDatabase", "PENDING -> ACTIVE",
                      wr.cause_boot(), ts=100.0),
        wr.make_event(1, 1, wr.LOAD, "UserCache", "PENDING -> ACTIVE",
                      wr.cause_requirements(
                          [{"component": "PgDatabase", "key": "db"}]), ts=100.1),
        wr.make_event(2, 1, wr.WITHDRAW, "UserCache", "ACTIVE -> PENDING",
                      wr.cause_provider_withdrawn("PgDatabase", "db"), ts=101.0),
        wr.make_event(3, 1, wr.WITHDRAW, "PgDatabase", "ACTIVE -> DISPOSED",
                      wr.cause_trigger("withdrawn by operator"), ts=101.1),
        wr.make_emit_event(4, 1, "UserCache", "Audit", "audit.write",
                           wr.cause_trigger("crossed by step-back to 2"),
                           ts=101.2),
    ]


def test_emit_event_maps_to_a_plain_span_without_regressing_the_cascade():
    spans = otel.build_spans(_v2_trace_with_emit())
    by_id = _by_id(spans)
    # the four cascade spans are exactly as before (root + 5 total now)
    assert by_id["s0"].parent_id == otel.ROOT_SPAN_ID
    assert by_id["s2"].parent_id == "s3"  # provider-withdrawn link intact
    # the emit event became its own span: unset status, capability + key carried
    emit = by_id["s4"]
    assert emit.kind == wr.EMIT
    assert emit.status == otel.STATUS_UNSET       # nothing settled
    assert emit.attributes["revl.capability"] == "Audit"
    assert emit.attributes["revl.emission.key"] == "audit.write"


def test_ts_is_surfaced_on_spans_when_present():
    spans = _by_id(otel.build_spans(_v2_trace_with_emit()))
    assert spans["s0"].attributes["revl.ts"] == 100.0
    # a v1 event (no ts) simply has no revl.ts attribute
    v1 = _by_id(otel.build_spans(_cascade_trace()))
    assert "revl.ts" not in v1["s0"].attributes


def test_failure_code_is_surfaced_on_the_cause_event():
    events = [
        wr.make_event(0, 1, wr.LOAD, "Svc", "PENDING -> ACTIVE",
                      wr.cause_boot()),
        wr.make_event(1, 1, wr.WITHDRAW, "Svc", "ACTIVE -> FAILED",
                      wr.cause_trigger("crashed", code="G7")),
    ]
    spans = _by_id(otel.build_spans(events))
    failed = spans["s1"]
    assert failed.status == otel.STATUS_ERROR
    assert failed.events[0].attributes["revl.cause.code"] == "G7"
    # a FAILED with no code carries no fabricated one
    nocode = _by_id(otel.build_spans([
        wr.make_event(0, 1, wr.WITHDRAW, "Svc", "ACTIVE -> FAILED",
                      wr.cause_trigger("crashed")),
    ]))
    assert "revl.cause.code" not in nocode["s0"].events[0].attributes


def test_v1_trace_builds_the_same_spans_as_before():
    """A literal v1 trace (v:1, no ts/code/emit) must map identically — no new
    attributes appear, and the span count/shape is unchanged."""
    v1 = [
        {"v": 1, "seq": 0, "event": "load", "component": "A",
         "transition": "PENDING -> ACTIVE", "cause": {"kind": "boot"}},
        {"v": 1, "seq": 1, "event": "withdraw", "component": "A",
         "transition": "ACTIVE -> DISPOSED",
         "cause": {"kind": "trigger", "detail": "op"}},
    ]
    spans = otel.build_spans(v1)
    assert len(spans) == 3  # root + 2
    for s in spans[1:]:
        assert "revl.ts" not in s.attributes
        assert "revl.capability" not in s.attributes
        assert "revl.cause.code" not in s.events[0].attributes


# ---- the optional-dependency contract ------------------------------------


def test_export_without_sdk_is_a_structured_no_op(monkeypatch):
    """Force the "SDK absent" path regardless of the environment: it must
    return a clear no-op result, never raise ImportError, and never touch a
    core import."""
    monkeypatch.setattr(otel, "otel_available", lambda: False)
    result = otel.export_to_otel(_cascade_trace())
    assert result["exported"] is False
    assert "revl[otel]" in result["reason"]
    # it still reports how many spans it *would* have emitted (root + 4)
    assert result["spans"] == 5


def test_cli_without_sdk_prints_the_span_mapping(tmp_path, monkeypatch):
    path = tmp_path / "run.jsonl"
    wr.write_trace(_cascade_trace(), str(path))
    monkeypatch.setattr(otel, "otel_available", lambda: False)
    rc = otel.main([str(path)])
    assert rc == 0  # a useful, non-error result even without the SDK


# ---- the with-SDK path (skips cleanly when the optional dep is absent) ----

HAVE_OTEL = otel.otel_available()
needs_otel = pytest.mark.skipif(
    not HAVE_OTEL,
    reason="needs the OpenTelemetry SDK (pip install revl[otel])")


@needs_otel
def test_export_through_the_sdk_emits_spans_events_and_links():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    result = otel.export_to_otel(_cascade_trace(), tracer_provider=provider)
    provider.force_flush()
    assert result["exported"] is True

    finished = {s.name: s for s in exporter.get_finished_spans()}
    # run root + one span per event
    assert "revl run" in finished
    assert "UserCache withdraw" in finished

    uc_withdraw = finished["UserCache withdraw"]
    # the cause is a span event
    assert any(e.name == f"cause:{wr.PROVIDER_WITHDRAWN}"
               for e in uc_withdraw.events)
    # the provision edge is a span link, and every span shares one trace
    assert len(uc_withdraw.links) == 1
    trace_ids = {s.context.trace_id for s in finished.values()}
    assert len(trace_ids) == 1


@needs_otel
def test_export_of_a_v2_trace_with_an_emit_event_does_not_regress():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    result = otel.export_to_otel(_v2_trace_with_emit(), tracer_provider=provider)
    provider.force_flush()
    assert result["exported"] is True
    finished = {s.name: s for s in exporter.get_finished_spans()}
    assert "revl run" in finished
    # the emit event exported as its own span alongside the cascade
    assert "UserCache emit" in finished
    assert finished["UserCache emit"].attributes["revl.capability"] == "Audit"


@needs_otel
def test_export_trace_file_reads_jsonl(tmp_path):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    path = tmp_path / "run.jsonl"
    wr.write_trace(_cascade_trace(), str(path))
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    result = otel.export_trace_file(str(path), tracer_provider=provider)
    provider.force_flush()
    assert result["exported"] is True
    assert len(exporter.get_finished_spans()) == 5

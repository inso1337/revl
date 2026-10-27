"""Issue #2730, slice 2: a teardown that leaves something behind is counted.

`revl run` proves "no residue" at teardown with a list of checks, and prints
the result, but the run trace carried no record of it: `revl metrics` could
not say how many runs ended with something not undone.

Teardown now writes one `residue` event to the trace when any check fails,
naming the failed checks (never their detail text), and `revl metrics --json`
counts those events under `residue`. A clean teardown writes nothing. The OTel
export maps the event to an error span without a component.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl import metrics as mt  # noqa: E402
from revl import otel as ot  # noqa: E402
from revl import run  # noqa: E402
from revl import why_runtime as wr  # noqa: E402


def _teardown_driver(trace_path, *, inverse_residue=(), registry_size=0,
                     run_header=None):
    """A driver stubbed down to what `_teardown` reads, so the real teardown
    and its real trace write run without a live composition."""
    driver = run._Driver.__new__(run._Driver)
    driver.ir = {}
    driver.generation = 1
    driver._events = []
    driver._seq = 0
    driver._run_header = dict(run_header or {})
    driver.trace_path = str(trace_path)
    driver.recorder = None
    driver.wal_path = None
    driver._baseline_disposables = 0
    driver._baseline_hooks = ()
    driver._compensation_residue = list(inverse_residue)
    driver._hooks = lambda: ()
    driver._log = lambda *a, **k: None
    driver.runtime = SimpleNamespace(set_trace=lambda t: None)
    driver.root = SimpleNamespace(
        registry=SimpleNamespace(size=registry_size),
        reflect=SimpleNamespace(store={}),
        fiber=SimpleNamespace(_disposables=SimpleNamespace(length=0)))

    async def dispose_all(ir, **kwargs):
        return None
    driver._dispose_all = dispose_all
    return driver


def _teardown(driver):
    asyncio.run(driver._teardown())
    return wr.read_trace(driver.trace_path)


def test_a_teardown_with_residue_writes_one_residue_event(tmp_path):
    driver = _teardown_driver(tmp_path / "run.jsonl",
                              inverse_residue=[{"step": "x"}],
                              registry_size=2)
    events = _teardown(driver)
    residue = [e for e in events if e["event"] == wr.RESIDUE]
    assert len(residue) == 1
    assert residue[0]["checks"] == ["registry", "inverse residue"]
    assert "transition" not in residue[0] and "component" not in residue[0]
    assert residue[0]["cause"]["kind"] == wr.TRIGGER


def test_a_clean_teardown_writes_no_residue_event(tmp_path):
    events = _teardown(_teardown_driver(tmp_path / "run.jsonl"))
    assert [e for e in events if e["event"] == wr.RESIDUE] == []
    assert mt.metrics_from_file(str(tmp_path / "run.jsonl"))["residue"] == \
        {"total": 0, "by_check": {}}


def test_metrics_json_counts_the_forced_residue(tmp_path):
    path = tmp_path / "run.jsonl"
    _teardown(_teardown_driver(path, inverse_residue=[{"step": "x"}]))
    document = json.loads(json.dumps(mt.metrics_from_file(str(path))))
    assert document["residue"] == {"total": 1,
                                   "by_check": {"inverse residue": 1}}
    assert "residue incidents (total 1)" in mt.render(document)


def test_the_residue_event_carries_the_attested_header_when_first(tmp_path):
    header = {"compositionHash": "a" * 64}
    events = _teardown(_teardown_driver(
        tmp_path / "run.jsonl", registry_size=1, run_header=header))
    assert events[0]["event"] == wr.RESIDUE and events[0]["run"] == header
    assert mt.compute_metrics(events)["run"] == header


def test_the_residue_span_is_an_error_span_without_a_component(tmp_path):
    events = _teardown(_teardown_driver(tmp_path / "run.jsonl",
                                        registry_size=1))
    spans = [s for s in ot.build_spans(events) if s.kind == wr.RESIDUE]
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "residue"
    assert span.status == ot.STATUS_ERROR
    assert span.attributes["revl.residue.checks"] == ["registry"]
    assert "revl.component" not in span.attributes


def test_a_trace_without_residue_events_counts_zero():
    events = [wr.make_event(0, 1, wr.LOAD, "A", "? -> ACTIVE",
                            wr.cause_boot(), ts=1.0)]
    assert mt.compute_metrics(events)["residue"] == \
        {"total": 0, "by_check": {}}

"""Issue #2527: host-reported usage NUMBERS are typed, not copied.

The trace hop and the WAL model-decision record carry `tokensIn`, `tokensOut`
and `cost.amount` as numbers. Every other host-reported leaf was already
redacted for the trace, but these three were copied verbatim, so a host body
that echoed a registered `Secret[T]` value into one of them put the value into
both artifacts, inside a field a reader trusts as a count.

Two reads of the same return, deliberately different at the edge:

* the WAL's read (`runtime.revl_host_usage`) COERCES all three to numbers and
  refuses a non-number by field name (`RevlUsageRefused`), so the durable
  record is never written with text in a numeric slot;
* `runtime.revl_model_hop` keeps a `redact_text` backstop for every string leaf
  it is handed, because the trace's read (`revl.run._host_usage`) is a
  best-effort passthrough and both sinks serialize the hop's result.

An ordinary numeric return is byte-identical through both, so the exact-value
scrub cannot fire on a run that registered no secret.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

import confidential  # noqa: E402
import replay as rp  # noqa: E402
import runtime as rt  # noqa: E402
from revl import run  # noqa: E402
from revl import wal as wal_core  # noqa: E402
from revl import why_runtime as wr  # noqa: E402

SECRET = "sk-live-2527-usage-do-not-leak"
REDACTED = "<redacted:secret>"


@pytest.fixture(autouse=True)
def _fresh_state():
    """The registered-secret set and the fiber-local model-call registers are
    process state; each test starts and ends from empty."""
    confidential.forget_secret_values()
    rt.revl_reset_run_trace_state()
    yield
    confidential.forget_secret_values()
    rt.revl_reset_run_trace_state()


def _host_return(**overrides):
    raw = {"tag": "ok", "model": "openai:gpt-4o-2024-08-06",
           "tokensIn": 1204, "tokensOut": 88,
           "cost": {"amount": 0.0121, "currency": "USD"}}
    raw.update(overrides)
    return raw


def _trace_crossing(tmp_path, host):
    """One model completion recorded through the driver's real emit glue, then
    written by the real trace writer. Returns the hop payload and the JSONL."""
    rt.validate_retry(host, budget=0, schema={"type": "object"}, where="Agent")
    driver = run._Driver.__new__(run._Driver)
    driver.runtime = rt
    driver._events = []
    driver._seq = 0
    driver.generation = 1
    payload = driver._model_crossing_payload(
        args=["prompt"], arg_origins={"input"}, taint_engaged=False)
    driver._record_emit("Agent", "model", "Model.complete",
                        wr.cause_trigger("decision point"), llm=payload)
    path = tmp_path / "trace.jsonl"
    wr.write_trace(driver._events, str(path))
    return payload, path.read_text(encoding="utf-8")


def _wal_crossing(tmp_path, host, name="run.wal"):
    """One model completion through a WAL-attached recorder, the way a live run
    wires it. Returns the refusal (or None), the WAL's raw text, and the
    model-decision records it holds."""
    path = str(tmp_path / name)
    wal = rp.WriteAheadLog(path, ir={}, generation=1).open()
    timeline = rp.Timeline("AgentLoop")
    timeline.attach_wal(wal, {})

    def host_model():
        timeline.record_emission("Model", "complete", ("system", "ask"),
                                 "model", ("agent.rvl", 12))
        return host()

    refusal = None
    try:
        rt.validate_retry(host_model, budget=0, schema={"type": "object"},
                          where="AgentLoop")
    except rt.RevlUsageRefused as caught:
        refusal = caught
    wal.close()
    decisions = [record for record in wal_core.read_wal(path)["records"]
                 if record.get("record") == "model-decision"]
    return refusal, Path(path).read_text(encoding="utf-8"), decisions


def _usage_of(llm):
    """The three numeric leaves, the projection a reader compares and sums."""
    return {"tokensIn": llm["tokensIn"], "tokensOut": llm["tokensOut"],
            "cost.amount": llm["cost"]["amount"]}


def test_a_secret_in_the_usage_fields_reaches_neither_sink(tmp_path):
    """The reported leak: one host double echoes a registered secret into all
    three numeric leaves. The trace hop redacts each leaf, and the WAL refuses
    the crossing by field name before any model-decision record is written, so
    neither artifact on disk carries the value."""
    rt.mark_secret(SECRET)
    leaked = {"tokensIn": SECRET, "tokensOut": SECRET,
              "cost": {"amount": SECRET, "currency": "USD"}}

    payload, trace_text = _trace_crossing(
        tmp_path, lambda: _host_return(**leaked))
    assert SECRET not in trace_text
    assert _usage_of(payload) == {"tokensIn": REDACTED, "tokensOut": REDACTED,
                                  "cost.amount": REDACTED}
    # the trace was actually written, with the redaction in the numeric slots
    assert f'"tokensIn":"{REDACTED}"' in trace_text

    refusal, wal_text, decisions = _wal_crossing(
        tmp_path, lambda: _host_return(**leaked))
    assert isinstance(refusal, rt.RevlUsageRefused)
    assert "tokensIn" in str(refusal)
    assert SECRET not in str(refusal)
    assert SECRET not in wal_text
    assert decisions == []


def test_numeric_usage_is_byte_identical_when_no_secret_is_registered(tmp_path):
    """The other half of the contract: with no secret registered the scrub is a
    no-op and the numbers reach both sinks exactly as the host reported them,
    ints still ints and the amount still the same float."""
    payload, trace_text = _trace_crossing(
        tmp_path, lambda: _host_return())

    refusal, wal_text, decisions = _wal_crossing(
        tmp_path, lambda: _host_return())
    assert refusal is None
    (decision,) = decisions

    assert _usage_of(payload) == {"tokensIn": 1204, "tokensOut": 88,
                                  "cost.amount": 0.0121}
    assert _usage_of(decision["llm"]) == _usage_of(payload)
    assert json.dumps(_usage_of(decision["llm"]), sort_keys=True) == \
        json.dumps(_usage_of(payload), sort_keys=True)
    assert SECRET not in trace_text and SECRET not in wal_text
    # the types survive too: a count stays an int, not a coerced float
    assert isinstance(payload["tokensIn"], int)
    assert isinstance(payload["tokensOut"], int)
    assert isinstance(payload["cost"]["amount"], float)


@pytest.mark.parametrize("field,raw", [
    ("tokensIn", {"tokensIn": "1204"}),
    ("tokensOut", {"tokensOut": "88"}),
    ("cost.amount", {"cost": {"amount": "0.0121", "currency": "USD"}}),
])
def test_a_non_numeric_usage_field_is_refused_by_name(field, raw):
    """A JSON string is not a number even when it spells one: coercing it would
    copy a host-chosen digit string (a numeric secret included) into a count.
    The refusal names the field and never echoes the value."""
    with pytest.raises(rt.RevlUsageRefused) as exc:
        rt.revl_host_usage(raw)
    assert field in str(exc.value)
    assert "1204" not in str(exc.value)


def test_a_non_numeric_usage_value_is_not_echoed_in_the_refusal():
    """The refusal is a diagnostic, not a second sink: a secret refused as a
    token count must not reappear in the message that refuses it."""
    rt.mark_secret(SECRET)
    with pytest.raises(rt.RevlUsageRefused) as exc:
        rt.revl_host_usage({"tokensIn": SECRET})
    assert "tokensIn" in str(exc.value)
    assert SECRET not in str(exc.value)

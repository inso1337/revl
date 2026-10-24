"""Issue #2527: the run trace redacts host-reported string fields.

`revl run --trace` writes the lifecycle events to JSONL without passing them
through the redaction helpers the console trace uses (`runtime._record`), and
`revl_model_hop` copied host-reported strings (the model identifier, the cost
currency) into the `llm` payload verbatim. A host body that echoed a
registered `Secret[T]` value into one of those fields put it in the trace.

The fix funnels the host-reported STRING fields through
`confidential.redact_text`, the same exact-value scrub the console trace
applies at its one choke point. The match is against values a declared
`Secret[T]` marking registered, never a pattern, so an ordinary run's record
is byte-identical.
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
import runtime as rt  # noqa: E402
from revl import run  # noqa: E402
from revl import why_runtime as wr  # noqa: E402

SECRET = "sk-live-2527-do-not-leak"


@pytest.fixture(autouse=True)
def _clean_secrets():
    """The registered-secret set is process state; every test starts and ends
    from an empty registry so one test's marking cannot redact another's."""
    confidential.forget_secret_values()
    yield
    confidential.forget_secret_values()


def _hop(**overrides):
    kwargs = dict(model=f"openai:{SECRET}", tokens_in=12, tokens_out=3,
                  cost={"amount": 0.01, "currency": SECRET},
                  latency_seconds=0.5, attempts=1, attempt_ceiling=2,
                  verified_by=[])
    kwargs.update(overrides)
    return rt.revl_model_hop(**kwargs)


def test_host_reported_model_id_is_redacted():
    """The model identifier is the field a host body is most likely to echo;
    it is host-reported text and goes through the same scrub as the console."""
    rt.mark_secret(SECRET)
    payload = _hop()
    assert SECRET not in json.dumps(payload)
    assert payload["model"] == "openai:<redacted:secret>"
    # provenance is untouched: redaction changes the text, not who reported it
    assert payload["modelProvenance"] == "host-reported"


def test_host_reported_cost_currency_is_redacted():
    """The other string field on the hop. `amount` is a number and is untouched;
    a secret smuggled into the currency is not."""
    rt.mark_secret(SECRET)
    payload = _hop()
    assert SECRET not in json.dumps(payload)
    assert payload["cost"]["currency"] == "<redacted:secret>"
    assert payload["cost"]["amount"] == 0.01
    assert payload["cost"]["provenance"] == "host-reported"


def test_an_ordinary_run_is_byte_identical():
    """No registered secret: the hop assembly is exactly what it was, so a
    redaction that fires on an ordinary run would fail here."""
    payload = _hop(model="openai:gpt-4o", cost={"amount": 0.01, "currency": "USD"})
    assert payload["model"] == "openai:gpt-4o"
    assert payload["cost"]["currency"] == "USD"


def test_the_recorded_trace_event_carries_no_secret():
    """End to end on the trace path: a host return that echoes a secret into
    the model field, recorded through the driver's real emit glue, leaves no
    secret anywhere in the serialised `emit` record."""
    rt.mark_secret(SECRET)
    calls = {"n": 0}

    def host_model():
        calls["n"] += 1
        return {"tag": "ok", "model": f"openai:{SECRET}",
                "tokensIn": 4, "tokensOut": 2,
                "cost": {"amount": 0.002, "currency": SECRET}}

    validated = rt.validate_retry(host_model, budget=0, schema={"type": "object"},
                                  where="Agent")
    assert validated["tag"] == "ok"

    driver = run._Driver.__new__(run._Driver)
    driver.runtime = rt
    driver._events = []
    driver._seq = 0
    driver.generation = 1
    llm = driver._model_crossing_payload(args=["prompt"], arg_origins={"input"},
                                         taint_engaged=False)
    driver._record_emit("Agent", "model", "Model.complete",
                        wr.cause_trigger("decision point"), llm=llm)

    event = driver._events[-1]
    assert event["llm"]["model"] == "openai:<redacted:secret>"
    assert event["llm"]["cost"]["currency"] == "<redacted:secret>"
    assert SECRET not in json.dumps(event)

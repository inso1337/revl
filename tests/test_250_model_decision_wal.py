"""Item 250 Slice 3a: the LLM-aware WAL, one durable `model-decision` record per
model completion crossing (docs/design/250-session-branching.md, Slice 3a).

Item 121 put the model-hop vocabulary on the TRACE, read off the `validate_retry`
seam at step-back time; nothing reached the WAL, so a branch's model decisions
died with the process and `revl compare` could only list `decisionCause` as not
comparable. Slice 3a bridges the same observation into the WAL AT THE CROSSING:
the recorder publishes a fiber-local sink beside the item-242 crossing key when a
WAL is attached, and the seam that measures the completion consumes it.

What these pin:

* a model crossing through a WAL-attached timeline writes exactly one
  `model-decision` record, right after the crossing's own `effect` record, keyed
  on `{component, stepIndex}`, carrying the trace hop's `llm` payload with every
  provenance tag, and NO prompt/response text, no digest, no `producedSeq`;
* a non-model crossing writes nothing extra (absent by default: the WAL is
  byte-identical to a pre-3a one), and a completion with no WAL attached stays
  trace-only with the seam otherwise unchanged;
* under a validation retry the record names the LAST attempt's crossing with
  `attempts` counting every crossing; an exhausted budget still writes it, with
  `outcome: exhausted`;
* the record is invisible to the fork partition and to `recover` (it is a fact
  about a step, not an effect), the core reader and the py reader agree on it,
  `revl.wal.model_decisions` indexes it, and `revl.branch.compare` lists it per
  side while still saying what a comparison cannot say.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
BACKEND = ROOT / "backends" / "python"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import replay as rp  # noqa: E402
import runtime as rt  # noqa: E402
from revl import branch as branch_mod  # noqa: E402
from revl import run  # noqa: E402
from revl import wal as wal_core  # noqa: E402
from revl.recovery import recover  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_run_state():
    rt.revl_reset_run_trace_state()
    yield
    rt.revl_reset_run_trace_state()


def _host_return(model="openai:gpt-4o-2024-08-06"):
    return {"tag": "ok", "model": model, "tokensIn": 1204, "tokensOut": 88,
            "cost": {"amount": 0.0121, "currency": "USD"}}


def _durable_timeline(tmp_path, name="run.wal", component="AgentLoop"):
    """A timeline with a WAL attached, the way the recorder wires a live run."""
    path = str(tmp_path / name)
    wal = rp.WriteAheadLog(path, ir={}, generation=1).open()
    timeline = rp.Timeline(component)
    timeline.attach_wal(wal, {})
    return timeline, wal, path


def _complete(timeline, budget=0, host=None, args=("system", "ask")):
    """One model completion through the REAL nesting: the crossing is recorded
    from inside `validate_retry`'s `make_call`, then the seam measures it."""
    def host_model():
        timeline.record_emission("Model", "complete", args, "model",
                                 ("agent.rvl", 12))
        return host() if host is not None else _host_return()

    return rt.validate_retry(host_model, budget=budget,
                             schema={"type": "object"}, where="AgentLoop")


def _decisions(path):
    return [r for r in wal_core.read_wal(path)["records"]
            if r.get("record") == "model-decision"]


# ---------------------------------------------------------------------------
# 1. the record: written at the crossing, keyed on it, shaped like the trace hop
# ---------------------------------------------------------------------------


def test_a_model_crossing_writes_one_decision_record_after_its_effect(tmp_path):
    timeline, wal, path = _durable_timeline(tmp_path)
    _complete(timeline)
    wal.close()

    records = wal_core.read_wal(path)["records"]
    kinds = [r["record"] for r in records]
    assert kinds == ["effect", "model-decision"]
    effect, decision = records
    # keyed on the completion's own effect record, not on a seq of its own
    assert decision["component"] == effect["component"] == "AgentLoop"
    assert decision["stepIndex"] == effect["stepIndex"] == 0
    assert "seq" not in decision
    assert decision["outcome"] == "validated"


def test_the_record_carries_the_trace_hop_payload_with_provenance(tmp_path):
    timeline, wal, path = _durable_timeline(tmp_path)
    _complete(timeline)
    wal.close()

    (decision,) = _decisions(path)
    llm = decision["llm"]
    assert llm["model"] == "openai:gpt-4o-2024-08-06"
    assert llm["modelProvenance"] == "host-reported"
    assert llm["tokensIn"] == 1204 and llm["tokensOut"] == 88
    assert llm["usageProvenance"] == "host-reported"
    assert llm["cost"] == {"amount": 0.0121, "currency": "USD",
                           "provenance": "host-reported"}
    assert llm["latencySeconds"] >= 0.0
    assert llm["latencyProvenance"] == "revl-measured-bracket"
    assert llm["attempts"] == 1 and llm["attemptCeiling"] == 1
    assert llm["attemptsProvenance"] == "revl-controlled"
    # the SAME shape the trace hop has, minus the two fields the WAL cannot
    # honestly carry (the driver-gated digest and the trace seq)
    hop = rt.revl_model_hop(model="m", tokens_in=1, tokens_out=1, cost=None,
                            latency_seconds=0.0, attempts=1, attempt_ceiling=1,
                            verified_by=[])
    assert set(llm) - {"cost"} == set(hop)
    assert "promptDigest" not in llm and "producedSeq" not in llm


def test_no_prompt_or_response_text_reaches_the_wal(tmp_path):
    timeline, wal, path = _durable_timeline(tmp_path)

    def host():
        payload = _host_return()
        payload["text"] = "the model's full answer, which must never be durable"
        return payload

    _complete(timeline, host=host, args=("a very secret system prompt", "ask"))
    wal.close()

    blob = Path(path).read_text(encoding="utf-8")
    decision_lines = [line for line in blob.splitlines()
                      if '"record": "model-decision"' in line]
    assert len(decision_lines) == 1
    assert "full answer" not in decision_lines[0]
    assert "secret system prompt" not in decision_lines[0]
    assert "prompt" not in json.loads(decision_lines[0])["llm"]


def test_the_seam_still_stashes_the_observation_for_the_trace(tmp_path):
    """Slice 3a ADDS a durable copy; the trace's keyed take is untouched."""
    timeline, wal, _ = _durable_timeline(tmp_path)
    _complete(timeline)
    wal.close()
    obs = rt.revl_take_model_call(("AgentLoop", 0))
    assert obs is not None and obs[1] == 1
    assert rt.revl_take_model_call(("AgentLoop", 0)) is None


# ---------------------------------------------------------------------------
# 2. absent by default
# ---------------------------------------------------------------------------


def test_a_non_model_crossing_writes_nothing_extra(tmp_path):
    timeline, wal, path = _durable_timeline(tmp_path)
    timeline.record_emission("Report", "write", ("/tmp/out.txt",), "fs",
                             ("agent.rvl", 19))
    wal.close()
    assert [r["record"] for r in wal_core.read_wal(path)["records"]] == ["effect"]


def test_a_later_non_model_crossing_never_inherits_a_stale_sink(tmp_path):
    """The sink is consumed on write and re-published with every crossing, the
    same lifetime as the item-242 crossing key it rides beside. A model
    completion followed by a file write yields exactly one decision, on the
    completion; the write's own sink is published (the recorder cannot know at
    record time which crossing is a completion) and never fired."""
    timeline, wal, path = _durable_timeline(tmp_path)
    _complete(timeline)
    assert rt._revl_model_decision_sink.get() is None       # consumed on write
    timeline.record_emission("Report", "write", ("/tmp/out.txt",), "fs",
                             ("agent.rvl", 19))
    assert rt._revl_model_decision_sink.get() is not None   # fresh, unfired
    wal.close()
    assert [d["stepIndex"] for d in _decisions(path)] == [0]


def test_a_completion_with_no_wal_attached_stays_trace_only():
    timeline = rp.Timeline("AgentLoop")
    out = _complete(timeline)
    assert out["tag"] == "ok"
    assert rt._revl_model_decision_sink.get() is None
    assert rt.revl_take_model_call(("AgentLoop", 0)) is not None


def test_a_completion_with_no_recorded_crossing_stays_trace_only():
    """The seam's own unit-test shape: `make_call` records nothing. No sink was
    ever published, so nothing is written and nothing raises."""
    rt.validate_retry(lambda: _host_return(), budget=0,
                      schema={"type": "object"}, where="Agent")
    assert rt.revl_take_model_call() is not None


def test_a_wal_closed_before_the_seam_returns_is_treated_as_absent(tmp_path):
    """The sink checks `is_open` at write time: a completion whose WAL closed
    between the crossing and the measurement must not raise out of a completion
    that already succeeded."""
    timeline, wal, path = _durable_timeline(tmp_path)

    def host():
        wal.close()
        return _host_return()

    out = _complete(timeline, host=host)
    assert out["tag"] == "ok"
    assert _decisions(path) == []


def test_the_pre_3a_wal_bytes_are_unchanged_for_a_run_with_no_completion(tmp_path):
    """Byte-identity: a timeline that never crossed a model boundary writes the
    same WAL through a recorder that publishes sinks as one that does not."""
    timeline, wal, path = _durable_timeline(tmp_path)
    timeline.record_emission("Report", "write", ("/tmp/out.txt",), "fs",
                             ("agent.rvl", 19))
    wal.close()
    with_sink = Path(path).read_text(encoding="utf-8")

    other = str(tmp_path / "plain.wal")
    plain = rp.WriteAheadLog(other, ir={}, generation=1).open()
    plain.append_timeline(timeline)
    plain.close()
    assert with_sink == Path(other).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 3. retries and exhaustion
# ---------------------------------------------------------------------------


def test_a_retry_names_the_last_crossing_and_counts_every_attempt(tmp_path):
    timeline, wal, path = _durable_timeline(tmp_path)
    calls = {"n": 0}

    def host():
        calls["n"] += 1
        return "not-a-dict" if calls["n"] < 3 else _host_return()

    _complete(timeline, budget=2, host=host)
    wal.close()

    records = wal_core.read_wal(path)["records"]
    assert [r["record"] for r in records] == \
        ["effect", "effect", "effect", "model-decision"]
    (decision,) = _decisions(path)
    assert decision["stepIndex"] == 2          # the attempt that was measured
    assert decision["outcome"] == "validated"
    assert decision["llm"]["attempts"] == 3    # every crossing the retry made
    assert decision["llm"]["attemptCeiling"] == 3


def test_an_exhausted_budget_still_writes_the_decision(tmp_path):
    """The crossing happened and cost tokens; the WAL says so, with the outcome."""
    timeline, wal, path = _durable_timeline(tmp_path)
    with pytest.raises(rt.ResponseValidationError):
        _complete(timeline, budget=1, host=lambda: "never-an-object")
    wal.close()
    (decision,) = _decisions(path)
    assert decision["outcome"] == "exhausted"
    assert decision["stepIndex"] == 1
    assert decision["llm"]["attempts"] == 2 == decision["llm"]["attemptCeiling"]
    assert decision["llm"]["model"] is None   # a bare string reports no usage


def test_the_async_seam_writes_it_too(tmp_path):
    timeline, wal, path = _durable_timeline(tmp_path)

    async def host_model():
        timeline.record_emission("Model", "complete", ("system", "ask"),
                                 "model", ("agent.rvl", 12))
        return _host_return()

    asyncio.run(rt.validate_retry_async(host_model, budget=0,
                                        schema={"type": "object"},
                                        where="AgentLoop"))
    wal.close()
    assert [d["outcome"] for d in _decisions(path)] == ["validated"]


# ---------------------------------------------------------------------------
# 4. readers: invisible to partition and recover, indexed by the core, compared
# ---------------------------------------------------------------------------


def test_host_usage_agrees_between_the_seam_and_the_driver():
    """The WAL record and the trace hop must read the host's usage the same
    way; the driver's copy is `run._host_usage`, the seam's is
    `runtime.revl_host_usage`."""
    for raw in (None, "text", {}, _host_return(),
                {"model": "m", "usage": {"tokensIn": 3, "tokensOut": 4}},
                {"tokensIn": 1, "cost": "not-a-dict"},
                {"usage": "not-a-dict", "cost": {"amount": 1}}):
        assert run._host_usage(raw) == rt.revl_host_usage(raw), raw


def test_the_core_indexes_decisions_by_crossing(tmp_path):
    timeline, wal, path = _durable_timeline(tmp_path)
    _complete(timeline)
    timeline.record_emission("Report", "write", ("/tmp/out.txt",), "fs",
                             ("agent.rvl", 19))
    wal.close()
    index = wal_core.model_decisions(wal_core.read_wal(path)["records"])
    assert list(index) == [("AgentLoop", 0)]
    assert index[("AgentLoop", 0)]["outcome"] == "validated"
    assert wal_core.model_decisions([]) == {}


def test_the_record_is_invisible_to_the_partition_and_to_recover(tmp_path):
    timeline, wal, path = _durable_timeline(tmp_path)
    _complete(timeline)
    wal.commit_activation(components=["AgentLoop"])
    wal.close()

    doc = branch_mod.partition(path, at=-1)
    assert [e["label"] for e in doc["emissionsCrossed"]] == ["Model.complete"]
    assert len(doc["emissionsCrossed"]) == 1
    # a complete activation rolls forward exactly as one with no decision does
    assert recover(path)["verdict"] == "rolled-forward"


def _fork_pair(tmp_path, *, child, branch_name):
    """A parent frozen at step 1 and a branch of it whose tail holds one
    completion; mirrors the Slice 2 fixture but writes through the live seam."""
    parent = str(tmp_path / f"parent-{child}.wal")
    pw = rp.WriteAheadLog(parent, ir={}, generation=1).open()
    pw.record_fork_begin(parent="P", at=1, crossed=[], would_cross=[])
    pw.record_fork_complete(branch=child)
    pw.record_fork_frozen(parent="P", at=1)
    pw.close()

    timeline, wal, path = _durable_timeline(tmp_path, name=branch_name)
    wal.record_fork_branch(branch=child, parent="P", at=1, parent_wal=parent,
                           preserved={"capabilities": ["model"]},
                           not_preserved=[])
    _complete(timeline)
    wal.close()
    return path


def test_compare_lists_each_sides_decisions_and_still_says_what_it_cannot(tmp_path):
    first = _fork_pair(tmp_path, child="B1", branch_name="b1.wal")
    second = _fork_pair(tmp_path, child="B2", branch_name="b2.wal")
    doc = branch_mod.compare(first, second)
    assert doc["relation"] == "siblings" and doc["comparable"] is True
    for side in ("left", "right"):
        (decision,) = doc[side]["modelDecisions"]
        assert decision["label"] == "Model.complete"
        assert decision["model"] == "openai:gpt-4o-2024-08-06"
        assert decision["attempts"] == 1 and decision["outcome"] == "validated"
    assert doc["delta"]["modelDecisions"] == {"left": 1, "right": 1}
    # the honest half is unchanged in shape and now precise in wording
    assert [e["axis"] for e in doc["notComparable"]] == \
        ["decisionCause", "counterfactual"]
    assert "no prompt/response digest" in doc["notComparable"][0]["why"]
    text = branch_mod.render(doc)
    assert "1 model decision(s)" in text
    assert "model openai:gpt-4o-2024-08-06" in text

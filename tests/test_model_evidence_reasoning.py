"""The model's reasoning span on the WAL, and per-entry replay status
(issue #2808; `docs/design/536-model-decision-evidence.md`).

A model decision used to say what was asked and what was chosen, and nothing
about whether the model also reasoned in a span the host did not return. The
record was silent, and silence reads like a run that had nothing to hide.

What these pin, one section per acceptance clause:

  1. a host that reports reasoning TOKENS and returns no reasoning text yields
     a 3.0 record with `visibility: hidden` and the reported count;
  2. the verifier refuses a 3.0 record with the member missing, and a 2.0
     record still verifies under its own body;
  3. a reasoning digest over an input carrying a `secret` origin is refused,
     and the member builder suppresses it rather than handing it over;
  4. the replay plan marks the hidden entry `rederivable: false` and
     `fromRecord: true` when the answer was retained, and reports which
     entries were opaque and how many.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import model_evidence as me  # noqa: E402
from revl import replay_modes as rm  # noqa: E402

KEY = b"a reasoning-span evidence key"
D = me.digest

#: The acceptance case's member: a reported span with no returned text.
HIDDEN = me.reasoning_span(tokens=408, text="", origins=["input"])


def _members(**overrides):
    """A well-formed 3.0 decision. Overrides replace members."""
    body = dict(
        component="AgentLoop",
        step_index=0,
        role="cloud",
        residence="off_device",
        model_digest=D("nanojev-0.4-weights"),
        model_digest_basis="declared",
        placement_digest=D("h100 / bf16"),
        prompt_binding={"mode": "content-addressed",
                        "value": D("classify this ticket"), "reason": None},
        template_digest=D("classify {ticket}"),
        origins=["input"],
        reasoning=me.reasoning_span(tokens=408, text="", origins=["input"]),
        candidates=[D("candidate a"), D("candidate b")],
        chosen=1,
        outcome="validated",
        sampling={"temperature": 0.2, "top_p": 0.95, "top_k": 40,
                  "seed": 20260920, "max_tokens": 512, "stop_digest": None},
        policy_digest=D("route model on classify { confidential -> local }"),
        fallback_depth=0,
        retained=None,
        recorded_at="2026-11-20T00:00:00+00:00",
    )
    body.update(overrides)
    return body


def sealed(**overrides):
    return me.seal(KEY, **_members(**overrides))


# ---------------------------------------------------------------------------
# 1. a reported span with no returned text is `hidden`, with its count
# ---------------------------------------------------------------------------

def test_a_host_that_reports_tokens_and_returns_no_text_is_hidden():
    """The acceptance case: the host reports a reasoning token count and no
    reasoning text. The member says the span existed, how big the host says it
    was, and nothing about what was in it."""
    span = me.reasoning_span(tokens=408, text="", origins=["input"])
    assert span["visibility"] == "hidden"
    assert span["tokens"] == {"value": 408, "provenance": "host-reported"}
    assert span["digest"] is None

    record = sealed(reasoning=span)
    assert me.verify(record, KEY).ok
    decision = me.reconstruct(record, KEY)["decision"]
    assert decision["reasoning"]["visibility"] == "hidden"
    assert decision["reasoning"]["tokens"]["value"] == 408
    assert decision["reasoning"]["digest"] is None


def test_a_returned_span_is_full_with_a_digest_and_never_the_text():
    """A full span is digested, not written. The digest exists; the reasoning
    text appears nowhere in the sealed record."""
    text = "weigh the two options before answering"
    span = me.reasoning_span(text=text, origins=["input"])
    assert span["visibility"] == "full"
    assert span["digest"] == D(text)
    record = sealed(reasoning=span)
    assert text not in json.dumps(record, sort_keys=True)


def test_a_host_that_reports_nothing_is_none_reported():
    span = me.reasoning_span(origins=["input"])
    assert span == {"visibility": "none-reported",
                    "tokens": {"value": None,
                               "provenance": "host-reported"},
                    "digest": None}
    assert me.verify(sealed(reasoning=span), KEY).ok


def test_a_summary_is_never_inferred_and_carries_its_digest():
    """`summary` is the one value only the host knows: the returned text is a
    summary rather than the span. revl reports it and binds the returned text
    by digest, not by claiming it is the full span."""
    span = me.reasoning_span(text="a short account of the reasoning",
                             visibility="summary", origins=["input"])
    assert span["visibility"] == "summary"
    assert span["digest"] == D("a short account of the reasoning")


# ---------------------------------------------------------------------------
# 2. the envelope: 3.0 requires the member; 2.0 keeps verifying
# ---------------------------------------------------------------------------

def test_the_verifier_refuses_a_3_0_record_without_the_reasoning_member():
    record = sealed()
    shortened = {k: v for k, v in record.items() if k != "reasoning"}
    # a non-holder is refused by the MAC first
    assert me.verify(shortened, KEY).link == me.EVIDENCE_SIGNATURE
    # a key holder who re-MACs it is refused by the 3.0 body
    shortened[me.SIGNATURE_FIELD] = me._sign(shortened, KEY)
    verdict = me.verify(shortened, KEY)
    assert not verdict.ok
    assert verdict.link == me.EVIDENCE_INCOMPLETE
    assert "reasoning" in verdict.reason


def test_an_old_version_record_is_accepted_under_its_own_body():
    """The compatibility promise: a record sealed before the member existed
    verifies under 2.0, and reconstruct says the member is not there rather
    than inventing a `none-reported` span it never carried."""
    body = {k: v for k, v in sealed().items() if k != "reasoning"}
    body.update(version="2.0", recorded_at="2026-11-01T00:00:00+00:00")
    body = {k: v for k, v in body.items() if k in me.BODY_MEMBERS_V2}
    body[me.SIGNATURE_FIELD] = me._sign(body, KEY)
    verdict = me.verify(body, KEY)
    assert verdict.ok, verdict.reason
    reading = me.reconstruct(body, KEY)
    assert reading["decision"]["reasoning"] is None


def test_the_reasoning_coherence_rules_are_refused():
    """The three facts in the member cannot contradict one another."""
    with pytest.raises(me.EvidenceRefused) as hidden_digest:
        sealed(reasoning={"visibility": "hidden", "digest": D("text"),
                          "tokens": {"value": 3,
                                     "provenance": "host-reported"}})
    assert hidden_digest.value.link == me.EVIDENCE_VOCABULARY

    with pytest.raises(me.EvidenceRefused) as nothing_with_count:
        sealed(reasoning={"visibility": "none-reported", "digest": None,
                          "tokens": {"value": 3,
                                     "provenance": "host-reported"}})
    assert nothing_with_count.value.link == me.EVIDENCE_VOCABULARY

    with pytest.raises(me.EvidenceRefused) as full_without_digest:
        sealed(reasoning={"visibility": "full", "digest": None,
                          "tokens": {"value": 3,
                                     "provenance": "host-reported"}})
    assert full_without_digest.value.link == me.EVIDENCE_VOCABULARY

    with pytest.raises(me.EvidenceRefused) as wrong_provenance:
        sealed(reasoning={"visibility": "none-reported", "digest": None,
                          "tokens": {"value": None,
                                     "provenance": "revl-measured"}})
    assert wrong_provenance.value.link == me.EVIDENCE_VOCABULARY


# ---------------------------------------------------------------------------
# 3. the digest is suppressed over a secret or confidential input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("origin,reason", (("secret", "secret-origin"),
                                           ("confidential",
                                            "confidential-origin")))
def test_a_reasoning_digest_over_a_disclosing_origin_is_suppressed(
        origin, reason):
    """A digest of reasoning derived from a secret prompt is a confirmation
    oracle, exactly as a content-addressed prompt binding would be. The builder
    suppresses it, and the verifier refuses a record that carries one anyway."""
    span = me.reasoning_span(tokens=64, text="the secret reasoning",
                             origins=sorted([origin, "input"]))
    assert span["visibility"] == "full"
    assert span["digest"] is None

    refused = dict(span, digest=D("the secret reasoning"))
    with pytest.raises(me.EvidenceRefused) as caught:
        sealed(origins=sorted([origin, "input"]), reasoning=refused,
               prompt_binding={"mode": "suppressed", "value": None,
                               "reason": reason})
    assert caught.value.link == me.EVIDENCE_DISCLOSURE

    # and the suppressed member itself seals: the visibility and the size are
    # still recorded, only the oracle is not
    record = sealed(origins=sorted([origin, "input"]), reasoning=span,
                    prompt_binding={"mode": "suppressed", "value": None,
                                    "reason": reason})
    assert me.verify(record, KEY).ok
    assert record["reasoning"]["visibility"] == "full"
    assert record["reasoning"]["digest"] is None


# ---------------------------------------------------------------------------
# 4. per-entry replay status over a WAL
# ---------------------------------------------------------------------------

def _decision(step, evidence):
    return {"record": "model-decision", "component": "AgentLoop",
            "stepIndex": step, "outcome": "validated",
            "llm": {"model": "openai:gpt-4o",
                    "modelProvenance": "host-reported",
                    "tokensIn": 1204, "tokensOut": 88,
                    "usageProvenance": "host-reported",
                    "latencySeconds": 1.84,
                    "latencyProvenance": "revl-measured-bracket",
                    "attempts": 1, "attemptCeiling": 3,
                    "attemptsProvenance": "revl-controlled",
                    "verifiedBy": []},
            "evidence": evidence}


def _wal(tmp_path, decisions):
    header = {"record": "header", "walVersion": 1, "generation": 1,
              "guarantee": "test"}
    lines = [json.dumps(header)] + [json.dumps(record)
                                    for record in decisions]
    path = tmp_path / "run.wal"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _retained(step=0, reasoning=HIDDEN):
    return sealed(
        step_index=step, reasoning=reasoning,
        retained={"prompt": "classify this ticket",
                  "completion": "the recorded answer",
                  "origins": ["input"]},
        retain_content=True)


def test_a_hidden_span_with_a_retained_answer_is_served_but_not_rederivable(
        tmp_path):
    """The acceptance case for Slice 2. The opaque span does not stop the
    recorded answer being served; it stops the answer being reproduced."""
    path = _wal(tmp_path, [_decision(0, _retained())])
    doc = rm.plan(path, evidence_key=KEY)
    (entry,) = doc["decisions"]
    assert entry["verified"] is True
    assert entry["reasoningVisibility"] == "hidden"
    assert entry["fromRecord"] is True
    assert entry["rederivable"] is False
    assert "hidden" in entry["rederivableReason"]
    assert doc["opaqueSpans"] == {"count": 1,
                                  "crossings": [["AgentLoop", 0]]}
    assert "opaque reasoning spans: 1 of 1" in rm.render(doc)


def test_a_none_reported_span_with_a_retained_answer_is_rederivable(tmp_path):
    """The control: with no opaque span the record is both servable and
    re-derivable, so `rederivable` is not simply always false."""
    span = me.reasoning_span(origins=["input"])
    path = _wal(tmp_path, [_decision(0, _retained(reasoning=span))])
    (entry,) = rm.plan(path, evidence_key=KEY)["decisions"]
    assert entry["reasoningVisibility"] == "none-reported"
    assert entry["fromRecord"] is True
    assert entry["rederivable"] is True
    assert entry["rederivableReason"] == ""


def test_an_unretained_answer_is_not_served_from_the_record(tmp_path):
    """`fromRecord` is about the ANSWER being on the record, not about the
    reasoning. Without a retained completion there is nothing to serve."""
    path = _wal(tmp_path, [_decision(0, sealed())])
    (entry,) = rm.plan(path, evidence_key=KEY)["decisions"]
    assert entry["reasoningVisibility"] == "hidden"
    assert entry["fromRecord"] is False
    assert entry["rederivable"] is False


def test_a_summary_span_is_opaque_and_a_pre_3_0_record_is_not_claimed(
        tmp_path):
    """A summary is opaque for the same reason a hidden span is, and an old
    record that says nothing about reasoning is not claimed rederivable: its
    silence is not evidence that no span was hidden."""
    summary = me.reasoning_span(text="a summary of the reasoning",
                                visibility="summary", origins=["input"])
    summary_record = _wal(tmp_path, [_decision(0, _retained(reasoning=summary))])
    (entry,) = rm.plan(summary_record, evidence_key=KEY)["decisions"]
    assert entry["reasoningVisibility"] == "summary"
    assert entry["rederivable"] is False

    old = {k: v for k, v in sealed().items() if k != "reasoning"}
    old.update(version="2.0")
    old = {k: v for k, v in old.items() if k in me.BODY_MEMBERS_V2}
    old[me.SIGNATURE_FIELD] = me._sign(old, KEY)
    old_path = _wal(tmp_path, [_decision(0, old)])
    (old_entry,) = rm.plan(old_path, evidence_key=KEY)["decisions"]
    assert old_entry["reasoningVisibility"] is None
    assert old_entry["rederivable"] is False
    assert "pre" in old_entry["rederivableReason"]

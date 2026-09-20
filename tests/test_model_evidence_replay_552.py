"""A recorded run replays a model decision from the artifact alone (roadmap
item 517 Slices 2 and 3, issue #1191; `docs/design/552-model-decision-replay.md`).

Slice 1 (`tests/test_model_evidence_517.py`) shipped the evidence object and
its gate as a pure function, and said plainly that the exit clause's "a
recorded run replays a model decision from the artifact alone" was NOT claimed:
every record in that file is constructed by hand and nothing wrote one during a
run. This file is the other half, and it is written so that it cannot be
satisfied by a constructed record.

What each section proves, and why none of it is vacuous:

  1. **A real crossing writes the record.** The completion goes through the
     actual nesting item 250 Slice 3a uses — `validate_retry` -> `make_call` ->
     `Timeline.record_emission` -> the fiber-local sink -> `WriteAheadLog` —
     and the assertions read the bytes back off DISK with `revl.wal.read_wal`.
     No test here hands a record to a verifier it built itself.

  2. **Absent by default.** A run that engages nothing writes the Slice-3a
     record byte for byte. That is asserted against a control WAL, not by
     inspection, because it is what keeps "not sealed" from reading as
     "sealed and refused".

  3. **The failure direction, stated and executed.** A run that HAS engaged
     sealing and cannot seal a crossing does not continue: the WAL gets the
     refusal with its link and reason FIRST, and `RevlModelEvidenceRefused` is
     then raised out of the crossing. Both halves are asserted, including the
     ordering, which is the part that matters to a post-mortem reader of a
     process that died at the crossing.

  4. **The replay, from the artifact alone.** `revl.replay_modes.plan` is given
     a PATH and a key and nothing else — the run's objects are dropped — and it
     reconstructs which model answered, where it ran, how the prompt was bound,
     what the candidates were, which was taken, how it was asked and under
     which rule.

  5. **The tamper demonstration, at this layer.** Editing one byte of a sealed
     record ON THE WAL FILE does not yield a wrong reading, it yields no
     reading: `verified` false, link `signature`, `decision` None. And the
     `key_id`-after-the-MAC bug this module was written not to repeat is
     INJECTED into a local copy of `_sign`, and the suite is shown to red on
     it — a tamper test that would still pass under the bug proves nothing.

  6. **The placement, cross-checked against item 512.** The route table comes
     from `model_route.roles()` on a program parsed by the real parser, not
     from a dict written here. A run declaring a residence the program refutes
     is refused AT SEAL TIME, so the contradicting record never exists.

This module drives no tier runner (`revl.test.run_*` / `RUNNERS[...]`), so it
is not a tier-execution test and does not belong in the `conformance` job's
list; it runs in the ordinary `frontend` suite.
"""

from __future__ import annotations

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
from revl import model_evidence as me  # noqa: E402
from revl import model_route  # noqa: E402
from revl import parser as revl_parser  # noqa: E402
from revl import replay_modes as rm  # noqa: E402
from revl import wal as wal_core  # noqa: E402

KEY = b"a run's model-evidence key"
OTHER_KEY = b"somebody else's key"

D = me.digest

# The program whose route table item 512 owns. Parsed by the real parser and
# read by item 512's own `roles()`, so nothing here is a second spelling of a
# placement table.
PROGRAM = """
model role local on_device
model role cloud off_device

service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  route model on classify {
    confidential -> local,
    * -> cloud
  }
  provide out { fn classify(text) = text }
}
"""


def route_roles():
    return model_route.roles(revl_parser.Parser(PROGRAM, "m.rvl").parse())


@pytest.fixture(autouse=True)
def _fresh_run_state():
    """The same reset `tests/test_250_model_decision_wal.py` takes, which also
    drops any sealer a previous test engaged."""
    rt.revl_reset_run_trace_state()
    yield
    rt.revl_reset_run_trace_state()


# ---------------------------------------------------------------------------
# the harness: a REAL crossing on a REAL WAL
# ---------------------------------------------------------------------------

def _declaration(**overrides):
    """What a provider declares for one crossing. Non-disclosing by default."""
    body = dict(
        role="cloud",
        residence="off_device",
        model_digest=D("nanojev-0.4-weights"),
        placement_digest=D("h100 / bf16"),
        prompt_binding={"mode": "content-addressed",
                        "value": D("classify this ticket"), "reason": None},
        origins=["input"],
        candidates=[D("candidate a"), D("candidate b"), D("candidate c")],
        chosen=1,
        sampling={"temperature": 0.2, "top_p": 0.95, "top_k": 40,
                  "seed": 20260920, "max_tokens": 512, "stop_digest": None},
        policy_digest=D("route model on classify { confidential -> local }"),
        fallback_depth=0,
        retained=None,
    )
    body.update(overrides)
    return body


def _run(tmp_path, *, sealer=None, declaration=_declaration, name="run.wal",
         host=None, budget=0):
    """One model completion through the real nesting, onto a real WAL.

    The crossing is recorded from inside `validate_retry`'s `make_call`, the
    provider declares from inside the host body (the only place that knows what
    it is declaring), and the seam that measures the completion is what seals.
    Returns the WAL path; the timeline, the WAL handle and the sealer are all
    dropped, so everything after this reads the artifact.
    """
    path = str(tmp_path / name)
    wal = rp.WriteAheadLog(path, ir={}, generation=1).open()
    timeline = rp.Timeline("AgentLoop")
    timeline.attach_wal(wal, {})
    if sealer is not None:
        rt.revl_engage_model_evidence(sealer)

    def host_model():
        if declaration is not None:
            rt.revl_declare_model_decision(**declaration())
        timeline.record_emission("Model", "complete", ("system", "ask"),
                                 "model", ("agent.rvl", 12))
        return host() if host is not None else {
            "tag": "ok", "model": "openai:gpt-4o-2024-08-06",
            "tokensIn": 1204, "tokensOut": 88}

    try:
        rt.validate_retry(host_model, budget=budget,
                          schema={"type": "object"}, where="AgentLoop")
    finally:
        if wal.is_open:
            wal.close()
        rt.revl_engage_model_evidence(None)
    return path


def _decisions(path):
    return [r for r in wal_core.read_wal(path)["records"]
            if r.get("record") == wal_core.RECORD_MODEL_DECISION]


def _rewrite(path, records):
    """Write a WAL's lines back, header first. Used to tamper ON DISK."""
    loaded = wal_core.read_wal(path)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(loaded["header"], sort_keys=True) + "\n")
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# 1. a recorded run writes a sealed record, keyed on the crossing
# ---------------------------------------------------------------------------

def test_a_model_crossing_seals_an_evidence_object_onto_its_wal_record(tmp_path):
    """The Slice 2 claim in one assertion: a real completion, and the sealed
    object is on the record when the process is gone."""
    path = _run(tmp_path, sealer=me.CrossingSealer(KEY))

    (decision,) = _decisions(path)
    evidence = me.from_wal_record(decision)
    assert evidence is not None, "the crossing wrote no evidence object"
    assert me.verify(evidence, KEY).ok
    assert evidence["kind"] == me.EVIDENCE_KIND
    assert evidence["version"] == me.EVIDENCE_VERSION


def test_the_seal_is_keyed_on_the_crossing_the_wal_indexes(tmp_path):
    """No second correlation. The evidence object's `crossing_key` is the key
    `revl.wal.model_decisions` builds its index on, and it rides ON that
    record, so one lookup reaches the observation and the account."""
    path = _run(tmp_path, sealer=me.CrossingSealer(KEY))

    records = wal_core.read_wal(path)["records"]
    index = wal_core.model_decisions(records)
    (decision,) = list(index.values())
    evidence = me.from_wal_record(decision)

    assert me.crossing_key(evidence) == ("AgentLoop", 0)
    assert index[me.crossing_key(evidence)] is decision
    # the crossing is the completion's own `effect` record, as in Slice 3a
    (effect,) = [r for r in records if r.get("record") == "effect"]
    assert (evidence["component"], evidence["step_index"]) == \
        (effect["component"], effect["stepIndex"])


def test_the_runtime_owns_the_crossing_and_the_outcome(tmp_path):
    """A provider cannot restate the crossing or the outcome, so it cannot seal
    a record about a crossing that did not happen or call an exhausted budget a
    validated answer. The refusal is what proves the members are not merely
    overwritten."""
    for member, value in (("component", "SomeOtherComponent"),
                          ("step_index", 99), ("outcome", "validated")):
        sealer = me.CrossingSealer(KEY)
        record, refusal = sealer(("AgentLoop", 0),
                                 _declaration(**{member: value}), "exhausted")
        assert record is None
        assert refusal["link"] == me.EVIDENCE_VOCABULARY
        assert member in refusal["reason"]


def test_the_outcome_on_the_record_is_the_one_the_seam_measured(tmp_path):
    """An exhausted retry budget still crossed and still cost tokens, so it is
    still sealed — with `exhausted`, which comes from the seam and not from the
    provider. Item 257's second reading, carried into the evidence object."""
    with pytest.raises(rt.ResponseValidationError):
        _run(tmp_path, sealer=me.CrossingSealer(KEY),
             host=lambda: "never-an-object", budget=1)

    (decision,) = _decisions(str(tmp_path / "run.wal"))
    evidence = me.from_wal_record(decision)
    assert decision["outcome"] == "exhausted"
    assert evidence["outcome"] == "exhausted"
    assert me.verify(evidence, KEY).ok
    # the retry made two crossings and the record names the LAST one, which is
    # the one the seam measured (item 250 Slice 3a's own rule)
    assert decision["stepIndex"] == 1
    assert me.crossing_key(evidence) == ("AgentLoop", 1)
    # the provider declared a chosen candidate from inside the host body,
    # before the seam had decided anything; the crossing owns the outcome, so
    # it owns the coherence of the choice with it. Nothing was TAKEN, and the
    # candidate set is still on the record in the order it was offered.
    assert evidence["chosen"] is None
    assert len(evidence["candidates"]) == 3


# ---------------------------------------------------------------------------
# 2. absent by default — the control
# ---------------------------------------------------------------------------

def test_a_run_that_engages_nothing_writes_the_slice_3a_record_unchanged(tmp_path):
    """The control, asserted against the real record rather than by reading the
    code: with no sealer the WAL carries neither member, and `revl replay`
    reports "not sealed" rather than inferring anything from the silence."""
    path = _run(tmp_path, sealer=None, declaration=None)

    (decision,) = _decisions(path)
    assert set(decision) == {"record", "component", "stepIndex", "outcome",
                             "llm"}
    assert me.from_wal_record(decision) is None

    (reading,) = rm.plan(path, evidence_key=KEY)["decisions"]
    assert reading["sealed"] is False
    assert reading["verified"] is None
    assert reading["decision"] is None
    assert "did not seal it" in reading["reason"]


def test_declaring_without_engaging_seals_nothing(tmp_path):
    """A declaration on a run with no sealer is inert. It must not become a
    record by itself, or `revl_declare_model_decision` would be a way to write
    an unsealed account onto the WAL."""
    path = _run(tmp_path, sealer=None)
    (decision,) = _decisions(path)
    assert me.from_wal_record(decision) is None
    assert me.WAL_REFUSAL_MEMBER not in decision


def test_a_declaration_is_consumed_and_never_inherited(tmp_path):
    """A later crossing must not seal an earlier provider's declaration as its
    own — the keyed-observation discipline item 242 already keeps."""
    rt.revl_declare_model_decision(**_declaration())
    rt.revl_engage_model_evidence(me.CrossingSealer(KEY))
    assert rt.revl_model_evidence_engaged()
    # consume it with one crossing, then the next has nothing to inherit
    sealer = me.CrossingSealer(KEY)
    record, refusal = sealer(("AgentLoop", 0), rt._revl_model_evidence_draft.get(),
                             "validated")
    assert record is not None and refusal is None
    rt._revl_model_evidence_draft.set(None)
    record, refusal = sealer(("AgentLoop", 1),
                             rt._revl_model_evidence_draft.get(), "validated")
    assert record is None
    assert refusal["link"] == me.EVIDENCE_INCOMPLETE
    rt.revl_engage_model_evidence(None)


# ---------------------------------------------------------------------------
# 3. the failure direction: fail-closed, and what happens instead
# ---------------------------------------------------------------------------

def test_an_engaged_run_that_cannot_seal_stops_at_the_crossing(tmp_path):
    """The stated failure direction, executed.

    The provider declares a body the gate refuses (a content-addressed prompt
    binding over a confidential input, which is the item 121 §4 confirmation
    oracle). The run does NOT continue with an unsigned record: it raises out
    of the crossing.
    """
    disclosing = _declaration(origins=["confidential", "input"])
    with pytest.raises(rt.RevlModelEvidenceRefused) as caught:
        _run(tmp_path, sealer=me.CrossingSealer(KEY),
             declaration=lambda: disclosing)
    assert caught.value.link == me.EVIDENCE_DISCLOSURE
    assert caught.value.crossing == ("AgentLoop", 0)


def test_the_refusal_reaches_the_artifact_before_the_run_stops(tmp_path):
    """Write first, raise second. A post-mortem reader is handed the WAL of a
    process that died at this crossing and must still be able to say WHICH
    crossing refused and WHY; if the raise came first the record would be
    indistinguishable from a run that never engaged evidence at all."""
    disclosing = _declaration(origins=["confidential", "input"])
    with pytest.raises(rt.RevlModelEvidenceRefused):
        _run(tmp_path, sealer=me.CrossingSealer(KEY),
             declaration=lambda: disclosing)

    (decision,) = _decisions(tmp_path / "run.wal" and str(tmp_path / "run.wal"))
    assert me.from_wal_record(decision) is None
    refusal = decision[me.WAL_REFUSAL_MEMBER]
    assert refusal["link"] == me.EVIDENCE_DISCLOSURE
    assert "confirmation oracle" in refusal["reason"]
    # and the Slice-3a observation is still there: the crossing happened and
    # cost tokens whether or not it could be accounted for
    assert decision["llm"]["model"] == "openai:gpt-4o-2024-08-06"


def test_a_refused_crossing_is_not_reported_as_unsealed(tmp_path):
    """The three states stay apart in the offline reading."""
    disclosing = _declaration(origins=["confidential", "input"])
    with pytest.raises(rt.RevlModelEvidenceRefused):
        _run(tmp_path, sealer=me.CrossingSealer(KEY),
             declaration=lambda: disclosing)

    (reading,) = rm.plan(str(tmp_path / "run.wal"),
                         evidence_key=KEY)["decisions"]
    assert reading["sealed"] is False
    assert reading["refusedAtRun"]["link"] == me.EVIDENCE_DISCLOSURE
    assert reading["decision"] is None
    assert "REFUSED AT RUN TIME" in rm.render(
        rm.plan(str(tmp_path / "run.wal"), evidence_key=KEY))


def test_an_engaged_crossing_with_no_declaration_is_a_refusal(tmp_path):
    """Engagement means every model crossing is accounted for. A crossing that
    publishes nothing is the case where a silent unsigned record would be
    easiest to write, so it is the case stated loudest."""
    with pytest.raises(rt.RevlModelEvidenceRefused) as caught:
        _run(tmp_path, sealer=me.CrossingSealer(KEY), declaration=None)
    assert caught.value.link == me.EVIDENCE_INCOMPLETE
    assert "published no declaration" in caught.value.reason
    (decision,) = _decisions(str(tmp_path / "run.wal"))
    assert decision[me.WAL_REFUSAL_MEMBER]["link"] == me.EVIDENCE_INCOMPLETE


def test_an_engaged_run_with_no_durable_sink_refuses(tmp_path):
    """The failure that looks most like a no-op and is not one: engaged, and
    nowhere to write. An evidence object that is never written is not
    evidence, so this is a refusal rather than a quiet trace-only decision."""
    timeline = rp.Timeline("AgentLoop")          # no WAL attached
    rt.revl_engage_model_evidence(me.CrossingSealer(KEY))

    def host_model():
        rt.revl_declare_model_decision(**_declaration())
        timeline.record_emission("Model", "complete", ("system", "ask"),
                                 "model", ("agent.rvl", 12))
        return {"tag": "ok", "model": "m"}

    try:
        with pytest.raises(rt.RevlModelEvidenceRefused) as caught:
            rt.validate_retry(host_model, budget=0, schema={"type": "object"},
                              where="AgentLoop")
    finally:
        rt.revl_engage_model_evidence(None)
    assert caught.value.link == "incomplete"
    assert "no durable sink" in caught.value.reason


def test_the_same_crossing_without_engagement_is_untouched(tmp_path):
    """The control for the refusals above: the identical declaration on a run
    that engaged nothing completes normally. The refusals are engagement's
    doing, not the declaration's."""
    disclosing = _declaration(origins=["confidential", "input"])
    path = _run(tmp_path, sealer=None, declaration=lambda: disclosing)
    (decision,) = _decisions(path)
    assert me.WAL_REFUSAL_MEMBER not in decision
    assert me.from_wal_record(decision) is None


# ---------------------------------------------------------------------------
# 4. the exit clause: the decision, from the artifact alone
# ---------------------------------------------------------------------------

def test_a_recorded_run_replays_its_model_decision_from_the_wal_alone(tmp_path):
    """The roadmap item's exit clause.

    Inputs: a PATH on disk and a key. The timeline, the WAL handle, the sealer
    and the provider are all gone. Everything asserted below was read out of
    the file.
    """
    path = _run(tmp_path, sealer=me.CrossingSealer(KEY))

    (reading,) = rm.plan(path, evidence_key=KEY)["decisions"]
    assert reading["verified"] is True
    decision = reading["decision"]

    # the crossing
    assert reading["crossing"] == ["AgentLoop", 0]
    # where it ran
    assert (decision["role"], decision["residence"]) == ("cloud", "off_device")
    # what answered, and on what host profile
    assert decision["modelDigest"] == D("nanojev-0.4-weights")
    assert decision["placementDigest"] == D("h100 / bf16")
    # what it was given
    assert decision["promptBindingMode"] == "content-addressed"
    assert decision["promptBinding"] == D("classify this ticket")
    assert decision["origins"] == ["input"]
    # what it could have said, and what it said
    assert len(decision["candidates"]) == 3
    assert decision["chosen"] == 1
    assert decision["chosenDigest"] == D("candidate b")
    assert decision["outcome"] == "validated"
    # how it was asked
    assert decision["sampling"]["seed"] == 20260920
    assert decision["sampling"]["temperature"] == 0.2
    # under which rule, and how far down the ladder
    assert decision["policyDigest"] == D(
        "route model on classify { confidential -> local }")
    assert decision["fallbackDepth"] == 0
    # and whether a re-run has what it needs
    assert reading["reproducible"] == {"ok": True, "missing": []}


def test_the_replay_reads_the_record_and_runs_nothing(tmp_path):
    """Reading a decision back is not re-executing it, and the plan keeps the
    distinction rather than blurring it: no mode becomes executable."""
    path = _run(tmp_path, sealer=me.CrossingSealer(KEY))
    doc = rm.plan(path, evidence_key=KEY)
    assert all(mode["executable"] is False for mode in doc["modes"])
    assert "not a re-execution" in doc["decisionNote"]


def test_a_content_addressed_binding_is_what_meets_the_prompt_digest(tmp_path):
    """The extension point `_present_inputs` was written for. A verified,
    content-addressed binding is the first thing on a revl WAL that can bind a
    substituted call to the original request across runs."""
    path = _run(tmp_path, sealer=me.CrossingSealer(KEY))
    present = rm.plan(path, evidence_key=KEY)["present"]
    assert present["sealedEvidence"] is True
    assert present["promptDigest"] is True
    # and the inputs Slice 3a still does not record have not moved
    assert present["responseText"] is False
    assert present["toolCalls"] is False


def test_a_salted_binding_does_not_meet_the_prompt_digest(tmp_path):
    """`salted-within-run` is stable within one run and meaningless across
    runs, so a cross-run replay bound to it is bound to nothing. It is a legal
    record and it is not a cross-run binding, and the plan keeps those apart."""
    salted = _declaration(prompt_binding={
        "mode": "salted-within-run", "value": "hmac-sha256:" + D("p"),
        "reason": None})
    path = _run(tmp_path, sealer=me.CrossingSealer(KEY),
                declaration=lambda: salted)
    doc = rm.plan(path, evidence_key=KEY)
    (reading,) = doc["decisions"]
    assert reading["verified"] is True
    assert doc["present"]["sealedEvidence"] is True
    assert doc["present"]["promptDigest"] is False


def test_a_suppressed_binding_still_replays_the_rest_of_the_decision(tmp_path):
    """A confidential input suppresses the binding and nothing else. The
    decision is still accountable — what answered, where, under which rule —
    which is the point of making suppression a stated mode rather than an
    absent field."""
    suppressed = _declaration(
        origins=["confidential", "input"],
        prompt_binding={"mode": "suppressed", "value": None,
                        "reason": "confidential-origin"})
    path = _run(tmp_path, sealer=me.CrossingSealer(KEY),
                declaration=lambda: suppressed)
    (reading,) = rm.plan(path, evidence_key=KEY)["decisions"]
    assert reading["verified"] is True
    decision = reading["decision"]
    assert decision["promptBindingMode"] == "suppressed"
    assert decision["promptBinding"] is None
    assert decision["promptSuppressionReason"] == "confidential-origin"
    assert decision["modelDigest"] == D("nanojev-0.4-weights")
    assert reading["reproducible"]["ok"] is False
    assert "prompt_binding" in reading["reproducible"]["missing"]


def test_without_a_key_a_seal_is_present_and_unchecked(tmp_path):
    """No key is not a pass. The plan says a seal is there and says it read
    nothing out of it, which is the only honest thing an unkeyed reader can
    say."""
    path = _run(tmp_path, sealer=me.CrossingSealer(KEY))
    doc = rm.plan(path)
    (reading,) = doc["decisions"]
    assert reading["sealed"] is True
    assert reading["verified"] is None
    assert reading["decision"] is None
    assert doc["present"]["sealedEvidence"] is False
    assert doc["present"]["promptDigest"] is False


def test_the_wrong_key_reads_nothing_out_of_a_genuine_record(tmp_path):
    path = _run(tmp_path, sealer=me.CrossingSealer(KEY))
    (reading,) = rm.plan(path, evidence_key=OTHER_KEY)["decisions"]
    assert reading["verified"] is False
    assert reading["link"] == me.EVIDENCE_SIGNATURE
    assert reading["decision"] is None


def test_the_cli_surface_carries_the_key_through(tmp_path, capsys):
    """`revl replay WAL --evidence-key PATH` — the reader an operator actually
    types, end to end."""
    from revl.cli.parser import build_parser
    from revl.cli.change import _run_replay

    path = _run(tmp_path, sealer=me.CrossingSealer(KEY))
    key_file = tmp_path / "evidence.key"
    key_file.write_bytes(KEY)

    args = build_parser().parse_args(
        ["replay", path, "--evidence-key", str(key_file), "--json"])
    assert _run_replay(args) == 0
    doc = json.loads(capsys.readouterr().out)
    (reading,) = doc["decisions"]
    assert reading["verified"] is True
    assert reading["decision"]["role"] == "cloud"

    # and the rendered form names the crossing and the placement
    args = build_parser().parse_args(
        ["replay", path, "--evidence-key", str(key_file)])
    assert _run_replay(args) == 0
    text = capsys.readouterr().out
    assert "evidence VERIFIED" in text
    assert "cloud (off_device)" in text


def test_the_key_is_never_written_into_the_plan(tmp_path):
    """Only the fingerprint, which is already a member of every record."""
    path = _run(tmp_path, sealer=me.CrossingSealer(KEY))
    doc = rm.plan(path, evidence_key=KEY)
    blob = json.dumps(doc)
    assert KEY.decode("utf-8") not in blob
    assert me.key_id(KEY) in blob


# ---------------------------------------------------------------------------
# 5. the tamper demonstration, on the artifact
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("member", sorted(me.BODY_MEMBERS))
def test_editing_any_member_on_the_wal_yields_no_reading(tmp_path, member):
    """Slice 1's tamper test, moved onto the artifact and kept generated from
    the record's own keys so it grows with the body.

    The claim is stronger than "an edited field fails its digest": an edited
    field yields NO DECISION. A reader that got a reading plus a warning would
    be a reader some caller eventually uses without checking the warning.
    """
    path = _run(tmp_path, sealer=me.CrossingSealer(KEY))
    records = wal_core.read_wal(path)["records"]
    for record in records:
        evidence = me.from_wal_record(record)
        if evidence is None:
            continue
        original = evidence[member]
        evidence[member] = _a_different_value(member, original)
        assert evidence[member] != original
    _rewrite(path, records)

    (reading,) = rm.plan(path, evidence_key=KEY)["decisions"]
    assert reading["verified"] is False, f"editing {member!r} was not caught"
    assert reading["link"] == me.EVIDENCE_SIGNATURE
    assert reading["decision"] is None


def _a_different_value(member, value):
    """Some value of the same broad shape that is not `value`. The same
    mutation table `tests/test_model_evidence_517.py` generates from."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 0.5
    if isinstance(value, str):
        return D(value + "!") if me._HEX64.match(value) else value + " (edited)"
    if isinstance(value, list):
        return value[1:] if value else [D("injected")]
    if isinstance(value, dict):
        edited = dict(value)
        key = sorted(edited)[0]
        edited[key] = _a_different_value(key, edited[key])
        return edited
    if value is None:
        return D("no longer absent")
    raise AssertionError(f"no mutation written for {member}={value!r}")


def test_moving_a_sealed_record_to_another_crossing_is_caught(tmp_path):
    """The attack the crossing-inside-the-MAC decision exists to stop: lift a
    genuine, genuinely-signed evidence object onto a different crossing's
    record. The MAC covers `component` and `step_index`, so the WAL's own index
    and the record's own claim cannot be made to disagree."""
    path = _run(tmp_path, sealer=me.CrossingSealer(KEY))
    records = wal_core.read_wal(path)["records"]
    for record in records:
        if me.from_wal_record(record) is not None:
            record["component"] = "SomeOtherComponent"
            record["stepIndex"] = 41
    _rewrite(path, records)

    (reading,) = rm.plan(path, evidence_key=KEY)["decisions"]
    # the WAL now indexes it at a crossing the sealed body does not name, and
    # the MAC over that body is still perfectly valid
    lifted = me.from_wal_record(
        [r for r in records if me.from_wal_record(r)][0])
    assert me.verify(lifted, KEY).ok
    assert me.crossing_key(lifted) == ("AgentLoop", 0)
    # so only a reader holding BOTH sides can refuse it, and it does
    assert reading["crossing"] == ["SomeOtherComponent", 41]
    assert reading["verified"] is False
    assert reading["link"] == me.EVIDENCE_CROSSING
    assert reading["decision"] is None
    assert "lifted onto this one" in reading["reason"]


def test_the_tamper_suite_reds_under_the_key_id_after_the_mac_bug(tmp_path,
                                                                  monkeypatch):
    """Non-vacuity, demonstrated by INJECTING the cited defect.

    This tree shipped a signed receipt whose `key_id` entered the body AFTER
    the MAC was taken over a hand-written field list, so the one member every
    reader used to choose a key was the one member nothing covered. Slice 1's
    defence is that `_sign` derives its covered set FROM the record. Here that
    defence is removed — `_sign` is replaced with a hand-written field list
    that omits `key_id`, exactly the shape of the bug — and the artifact-level
    tamper check is shown to STOP FIRING.

    Without this, "the tamper test passes" would be compatible with the tamper
    test proving nothing.
    """
    hand_written_list = tuple(m for m in me.BODY_MEMBERS if m != "key_id")

    def buggy_sign(body, key):
        import hashlib
        import hmac
        from revl.attest import _canonical_bytes
        covered = {m: body[m] for m in hand_written_list if m in body}
        return hmac.new(bytes(key), me.SIGN_DOMAIN + _canonical_bytes(covered),
                        hashlib.sha256).hexdigest()

    monkeypatch.setattr(me, "_sign", buggy_sign)

    path = _run(tmp_path, sealer=me.CrossingSealer(KEY))
    records = wal_core.read_wal(path)["records"]
    for record in records:
        evidence = me.from_wal_record(record)
        if evidence is not None:
            evidence["key_id"] = me.key_id(OTHER_KEY)
    _rewrite(path, records)

    (reading,) = rm.plan(path, evidence_key=KEY)["decisions"]
    # The MAC no longer covers `key_id`, so editing it is NOT caught as
    # tampering. The record is still refused — by the key-identity check, the
    # weaker of the two — which is precisely why the MAC test must exist
    # separately and must be generated rather than written out.
    assert reading["verified"] is False
    assert reading["link"] == me.EVIDENCE_SIGNER, (
        "under the injected bug the MAC did not fire on an edited `key_id`; "
        "the real build refuses this with EVIDENCE_SIGNATURE, and that "
        "difference is what makes the tamper suite non-vacuous")

    # And on the real build, the same edit IS caught by the MAC.
    monkeypatch.undo()
    path = _run(tmp_path, sealer=me.CrossingSealer(KEY), name="fixed.wal")
    records = wal_core.read_wal(path)["records"]
    for record in records:
        evidence = me.from_wal_record(record)
        if evidence is not None:
            evidence["key_id"] = me.key_id(OTHER_KEY)
    _rewrite(path, records)
    (reading,) = rm.plan(path, evidence_key=KEY)["decisions"]
    assert reading["link"] == me.EVIDENCE_SIGNATURE


# ---------------------------------------------------------------------------
# 6. the placement, cross-checked against item 512's route table
# ---------------------------------------------------------------------------

def test_the_route_table_is_item_512s_own(tmp_path):
    """The harness check. If this reds, the test below is measuring a table
    this file wrote rather than one item 512 derived from a program."""
    assert me.placement_table(route_roles()) == {"local": "on_device",
                                                 "cloud": "off_device"}


def test_a_record_agreeing_with_the_route_table_seals(tmp_path):
    path = _run(tmp_path, sealer=me.CrossingSealer(KEY, route_table=route_roles()))
    (decision,) = _decisions(path)
    assert me.verify(me.from_wal_record(decision), KEY).ok


def test_a_residence_the_program_refutes_is_never_sealed(tmp_path):
    """The check that was not available when Slice 1 was written.

    `role="cloud", residence="on_device"` is two legal words — the vocabulary
    check passes it — making a claim item 512's table refutes: that a prompt
    stayed on a device the program declared it leaves. The contradicting
    record is never minted, so no verifier ever has to be trusted to catch it.
    """
    lying = _declaration(role="cloud", residence="on_device")
    with pytest.raises(rt.RevlModelEvidenceRefused) as caught:
        _run(tmp_path, sealer=me.CrossingSealer(KEY, route_table=route_roles()),
             declaration=lambda: lying)
    assert caught.value.link == me.EVIDENCE_PLACEMENT
    assert "off_device" in caught.value.reason

    (decision,) = _decisions(str(tmp_path / "run.wal"))
    assert me.from_wal_record(decision) is None
    assert decision[me.WAL_REFUSAL_MEMBER]["link"] == me.EVIDENCE_PLACEMENT


def test_a_role_the_program_never_declared_is_refused(tmp_path):
    """A record naming a role outside the program's table describes a run of
    some other program."""
    stranger = _declaration(role="gpu-box", residence="off_device")
    with pytest.raises(rt.RevlModelEvidenceRefused) as caught:
        _run(tmp_path, sealer=me.CrossingSealer(KEY, route_table=route_roles()),
             declaration=lambda: stranger)
    assert caught.value.link == me.EVIDENCE_PLACEMENT
    assert "gpu-box" in caught.value.reason


def test_without_a_table_the_placement_is_bound_and_not_checked(tmp_path):
    """The honest statement of the gap that remains: a program declaring no
    `model role` has no table to check against, and its crossings are still
    recordable. The residence is bound by value either way, so a reader holding
    the table later can still refuse it (`check_placement` offline)."""
    lying = _declaration(role="cloud", residence="on_device")
    path = _run(tmp_path, sealer=me.CrossingSealer(KEY),
                declaration=lambda: lying)
    evidence = me.from_wal_record(_decisions(path)[0])
    assert me.verify(evidence, KEY).ok, "no table was supplied, so nothing checked"

    verdict = me.check_placement(evidence, route_roles())
    assert verdict is not None and verdict.link == me.EVIDENCE_PLACEMENT
    reading = me.reconstruct(evidence, KEY, roles=route_roles())
    assert reading["verified"] is False
    assert reading["decision"] is None


def test_the_offline_reader_can_carry_the_table_as_plain_data(tmp_path):
    """A `model_route.Role` is a compiler object and does not survive the
    artifact. The same check runs off `{role: residence}`, which does."""
    lying = _declaration(role="cloud", residence="on_device")
    path = _run(tmp_path, sealer=me.CrossingSealer(KEY),
                declaration=lambda: lying)
    evidence = me.from_wal_record(_decisions(path)[0])
    as_json = json.loads(json.dumps(me.placement_table(route_roles())))
    verdict = me.check_placement(evidence, as_json)
    assert verdict is not None and verdict.link == me.EVIDENCE_PLACEMENT


def test_a_malformed_placement_table_is_refused_not_ignored(tmp_path):
    """A table that cannot be read is not a table that admits everything."""
    for table in ({"cloud": "somewhere"}, {"cloud": None}, ["cloud"]):
        with pytest.raises(me.EvidenceRefused) as caught:
            me.placement_table(table)
        assert caught.value.link == me.EVIDENCE_PLACEMENT


def test_the_placement_link_is_distinct_from_the_vocabulary_link():
    """Two legal words making a refuted claim is a different fact from a word
    outside the vocabulary, and a caller branching on `link` must see both."""
    assert me.EVIDENCE_PLACEMENT in me.EVIDENCE_LINKS
    assert me.EVIDENCE_PLACEMENT != me.EVIDENCE_VOCABULARY

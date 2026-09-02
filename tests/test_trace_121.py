"""`revl trace` — the model hop as a first-class span (roadmap item 121,
docs/design/121-revl-trace.md), Slice 1.

Every mechanism the REVISED design (adversarial review 2026-09-01) makes
non-negotiable is pinned here on hand-built traces and direct runtime calls (no
live run needed), so the fixtures cannot drift from the real record shape:

* the widened `emit` record carries an `llm` payload + `activationId` (additive);
  a NON-model emit carries neither and is byte-identical to a pre-121 v2 emit;
* `producedSeq` is fiber-token-gated: present for a single-activation validated
  model->tool flow, OMITTED (never adjacency-guessed) when two activations of
  one component are live;
* the `promptDigest` is a SALTED HMAC (not the raw sha256) with a COARSE bucket
  (not an exact length); a secret/confidential arg SUPPRESSES it (no digest, hop
  still recorded) and NEVER refuses; a `Secret[T]`-receiving model op still
  compiles; the gate FAILS CLOSED when taint is disengaged;
* the OTel export flattens `llm` onto the span, carries NO prompt/response text,
  and adds a `model-produced` SpanLink ONLY when `producedSeq` is present.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl import otel as ot  # noqa: E402
from revl import run  # noqa: E402
from revl import trace as tr  # noqa: E402
from revl import why_runtime as wr  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

import runtime as rt  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_run_state():
    """Each test starts from a fresh per-run trace state (nonce + fiber
    registers), the run-start reset the driver performs."""
    rt.revl_reset_run_trace_state()
    yield
    rt.revl_reset_run_trace_state()


# ---------------------------------------------------------------------------
# 1. the widened emit record: llm + activationId additive; non-model byte-identical
# ---------------------------------------------------------------------------


def test_non_model_emit_is_byte_identical_to_pre_121():
    """A non-model emission passes no `llm` and no `activationId`, so its record
    is byte-identical to what `make_emit_event` produced before item 121 (the
    additive-v2 discipline metrics.py/otel.py already follow)."""
    cause = wr.cause_trigger("a filesystem write crossed")
    before = {
        "v": wr.SCHEMA_VERSION, "seq": 4, "gen": 1, "event": wr.EMIT,
        "component": "Reporter", "cause": cause, "capability": "fs",
        "key": "Report.write", "ts": 12.5,
    }
    got = wr.make_emit_event(4, 1, "Reporter", "fs", "Report.write", cause,
                             ts=12.5)
    assert got == before
    assert "llm" not in got and "activationId" not in got
    # and the serialised bytes match exactly (the durable artifact is identical)
    assert json.dumps(got, separators=(",", ":")) == \
        json.dumps(before, separators=(",", ":"))


def test_model_emit_llm_payload_shape_and_provenance():
    llm = rt.revl_model_hop(
        model="openai:gpt-4o", tokens_in=1204, tokens_out=88,
        cost={"amount": 0.0121, "currency": "USD"}, latency_seconds=1.84,
        attempts=1, attempt_ceiling=3, verified_by=["G9"])
    ev = wr.make_emit_event(7, 3, "AgentLoop", "model", "Model.complete",
                            wr.cause_trigger("decision point"), ts=99.0,
                            llm=llm, activation_id="AgentLoop#g3#a1")
    assert ev["activationId"] == "AgentLoop#g3#a1"
    assert ev["llm"]["latencyProvenance"] == "revl-measured-bracket"
    assert ev["llm"]["attemptsProvenance"] == "revl-controlled"
    assert ev["llm"]["modelProvenance"] == "host-reported"
    assert ev["llm"]["usageProvenance"] == "host-reported"
    assert ev["llm"]["cost"]["provenance"] == "host-reported"


# ---------------------------------------------------------------------------
# 2. validate_retry: the attempt count and the attempts-vs-ceiling oracle
# ---------------------------------------------------------------------------


def test_validate_retry_records_attempts_and_latency_bracket():
    """A `retry 2` completion that succeeds on the 3rd try records 3 attempts
    against the ceiling `budget + 1 = 3`, and a non-negative latency bracket."""
    calls = {"n": 0}

    def make_call():
        calls["n"] += 1
        # first two returns fail validation (a string is not an object),
        # the third validates
        return {"tag": "ok"} if calls["n"] >= 3 else "not-a-dict"

    schema = {"type": "object"}
    out = rt.validate_retry(make_call, budget=2, schema=schema, where="Model")
    assert out == {"tag": "ok"}
    latency, attempts, ceiling, raw = rt.revl_take_model_call()
    assert attempts == 3 and ceiling == 3
    assert latency >= 0.0
    # consumed: a second take is empty, so a later non-model emit inherits nothing
    assert rt.revl_take_model_call() is None


def test_attempt_ceiling_oracle_flags_an_over_retry():
    """The one model-hop number revl can check against a compile-time proof
    (§3.2): a recorded attempt count exceeding the static ceiling is a defect."""
    over = rt.revl_model_hop(
        model="m", tokens_in=1, tokens_out=1, cost=None, latency_seconds=0.1,
        attempts=5, attempt_ceiling=3, verified_by=[])
    ok = rt.revl_model_hop(
        model="m", tokens_in=1, tokens_out=1, cost=None, latency_seconds=0.1,
        attempts=2, attempt_ceiling=3, verified_by=[])
    doc = tr.compute_trace([
        wr.make_emit_event(1, 1, "A", "model", "M.c", wr.cause_boot(), llm=over),
        wr.make_emit_event(2, 1, "A", "model", "M.c", wr.cause_boot(), llm=ok),
    ])
    defects = tr.attempt_ceiling_defects(doc)
    assert len(defects) == 1
    assert defects[0]["kind"] == "attempt-ceiling-exceeded"
    assert defects[0]["seq"] == 1


# ---------------------------------------------------------------------------
# 3. producedSeq: the fiber-local value-flow token and its honest degrade
# ---------------------------------------------------------------------------


def test_produced_seq_present_for_single_activation_match():
    """A single-activation validated model->tool flow: the fiber holds the
    completion token and the downstream emit's activation matches, so
    `producedSeq` is present and correct."""
    rt.revl_note_validated_completion("AgentLoop#g1#a1", 7)
    assert rt.revl_produced_seq("AgentLoop#g1#a1") == [7]


def test_produced_seq_omitted_when_activation_ids_differ():
    """Two activations of the component are live: a token minted by activation a1
    must NOT be attributed to a2's emit. The edge honest-degrades to absent —
    NOT a guess, NOT trace adjacency (§4 attack 3, the NEW CRITICAL)."""
    rt.revl_note_validated_completion("AgentLoop#g1#a1", 7)
    assert rt.revl_produced_seq("AgentLoop#g1#a2") is None


def test_produced_seq_omitted_when_no_token():
    """A `Str`-returning non-validated completion mints no token (item 257 §1),
    so a later emit in the fiber has nothing to back-reference: omitted."""
    assert rt.revl_produced_seq("AgentLoop#g1#a1") is None


def test_produced_seq_isolated_across_fibers():
    """The token is fiber-local: a completion validated in one asyncio Task does
    not leak its token to a SIBLING task (each gets its own copied context), so a
    concurrent activation cannot cross-attribute."""
    import asyncio

    async def fiber(activation, seq, other):
        rt.revl_note_validated_completion(activation, seq)
        # this fiber sees its own token, and never the sibling's
        assert rt.revl_produced_seq(activation) == [seq]
        assert rt.revl_produced_seq(other) is None

    async def main():
        await asyncio.gather(
            fiber("Loop#g1#a1", 7, "Loop#g1#a2"),
            fiber("Loop#g1#a2", 11, "Loop#g1#a1"),
        )

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 4. the salted digest, the coarse bucket, and the suppression path
# ---------------------------------------------------------------------------


def test_digest_is_salted_not_raw_sha256():
    """The recorded digest is an HMAC keyed by the per-run nonce, so the RAW
    sha256 of the same canonical input does NOT match it — proving salting (§4
    attack 4b, the confirmation-oracle defeat)."""
    args = ["system prompt", "user question"]
    digest = rt.revl_prompt_digest(args, arg_origins=set(), taint_engaged=True)
    assert digest is not None
    assert digest["salted"].startswith("hmac-sha256:")
    raw = hashlib.sha256(rt._revl_canonical_args_bytes(args)).hexdigest()
    assert digest["salted"] != "hmac-sha256:" + raw
    assert digest["salted"] != raw


def test_digest_bucket_is_coarse_not_exact_length():
    """The record carries a coarse `bytesBucket`, never the exact byte count, so
    it cannot narrow a candidate search (§4 attack 4b)."""
    short = rt.revl_prompt_digest(["hi"], arg_origins=set(), taint_engaged=True)
    assert short["bytesBucket"] == "0-64"
    big = rt.revl_prompt_digest(["x" * 500], arg_origins=set(), taint_engaged=True)
    assert big["bytesBucket"] == "256-1k"
    # a bucket is a label, not a number
    assert not short["bytesBucket"].isdigit()


def test_digest_is_stable_within_run_and_varies_across_runs():
    """Salting preserves the digest's ONLY purpose — within-run "same prompt
    twice" equality — while the value varies across runs (a fresh nonce)."""
    args = ["a", "b"]
    d1 = rt.revl_prompt_digest(args, arg_origins=set(), taint_engaged=True)
    d2 = rt.revl_prompt_digest(args, arg_origins=set(), taint_engaged=True)
    assert d1["salted"] == d2["salted"]  # within one run: equal
    rt.revl_reset_run_trace_state()      # a new run mints a new nonce
    d3 = rt.revl_prompt_digest(args, arg_origins=set(), taint_engaged=True)
    assert d3["salted"] != d1["salted"]  # across runs: differs


def test_secret_arg_suppresses_digest_without_raising():
    """A `secret` origin at the digest position SUPPRESSES (returns None, the
    hop is still recorded) and NEVER raises (HIGH 2: suppression, not refusal)."""
    d = rt.revl_prompt_digest(["k"], arg_origins={"secret"}, taint_engaged=True)
    assert d is None


def test_confidential_arg_suppresses_digest_without_raising():
    d = rt.revl_prompt_digest(["t"], arg_origins={"confidential"},
                              taint_engaged=True)
    assert d is None


def test_digest_fails_closed_when_taint_disengaged():
    """Fail-closed default: with the analysis unavailable/disengaged the flow is
    unproven and the digest is suppressed, even for otherwise-clean args."""
    assert rt.revl_prompt_digest(["clean"], arg_origins=None,
                                 taint_engaged=True) is None
    assert rt.revl_prompt_digest(["clean"], arg_origins=set(),
                                 taint_engaged=False) is None


def test_clean_args_yield_a_digest_when_taint_engaged():
    d = rt.revl_prompt_digest(["clean prompt"], arg_origins={"model", "input"},
                              taint_engaged=True)
    assert d is not None and d["provenance"] == "revl-side-args"


def test_suppression_records_the_hop_with_the_digest_absent():
    """The hop is still traced when the digest is suppressed — just without a
    `promptDigest` field (the reader and otel copy only present fields)."""
    llm = rt.revl_model_hop(
        model="m", tokens_in=1, tokens_out=1, cost=None, latency_seconds=0.1,
        attempts=1, attempt_ceiling=3, verified_by=["G9"],
        prompt_digest=rt.revl_prompt_digest(["k"], arg_origins={"secret"},
                                            taint_engaged=True))
    assert "promptDigest" not in llm  # suppressed
    ev = wr.make_emit_event(7, 1, "A", "model", "M.c", wr.cause_boot(), llm=llm)
    hops = tr.compute_trace([ev])["modelHops"]
    assert len(hops) == 1 and "promptDigest" not in hops[0]  # hop still recorded


def test_secret_receiving_model_op_still_compiles():
    """A model op whose prompt param is declared `Secret[T]` is a legal program;
    tracing must not make it uncompilable. The default digest path adds NO
    compile refusal (only the deferred `--capture-prompts` text sink would)."""
    src = (
        "extern emission[payment.charge] fn charge(a: Str) -> Secret[Str] "
        "= @py { return a }\n"
        "extern emission[model.complete] fn prompt(p: Secret[Str]) -> Str "
        "= @py { return \"\" }\n"
        "service Ops { emission fn go(u: Str) -> Int }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops {\n"
        "    fn go(u) {\n"
        "      let t = charge(u)\n"
        "      let r = prompt(t)\n"
        "      return 0\n"
        "    }\n"
        "  }\n}\n")
    compile_source(src, "secret_model.rvl")  # compiles — the receiver declared it


# ---------------------------------------------------------------------------
# 5. the OTel mapping: flattened llm, the gated SpanLink, no text ever
# ---------------------------------------------------------------------------


def _model_event(seq, activation, llm):
    return wr.make_emit_event(seq, 1, "AgentLoop", "model", "Model.complete",
                              wr.cause_boot(), ts=float(seq), llm=llm,
                              activation_id=activation)


def test_otel_flattens_llm_with_genai_names_and_provenance():
    llm = rt.revl_model_hop(
        model="openai:gpt-4o", tokens_in=1204, tokens_out=88,
        cost={"amount": 0.0121, "currency": "USD"}, latency_seconds=1.84,
        attempts=1, attempt_ceiling=3, verified_by=["G9"])
    spans = ot.build_spans([_model_event(7, "AgentLoop#g1#a1", llm)])
    hop = next(s for s in spans if s.kind == wr.EMIT)
    a = hop.attributes
    assert a["gen_ai.request.model"] == "openai:gpt-4o"
    assert a["gen_ai.usage.input_tokens"] == 1204
    assert a["gen_ai.usage.output_tokens"] == 88
    assert a["revl.llm.cost"] == 0.0121
    assert a["revl.llm.latency.provenance"] == "revl-measured-bracket"
    assert a["revl.llm.cost.provenance"] == "host-reported"
    assert a["revl.llm.attempts.provenance"] == "revl-controlled"
    assert a["revl.activation.id"] == "AgentLoop#g1#a1"


def test_otel_never_carries_prompt_or_response_text():
    """No prompt/response text becomes a span attribute (§3.1 rule 2). Even with
    a digest present, only the salted hash + coarse bucket ride — never the
    GenAI `gen_ai.prompt`/`gen_ai.completion` text keys."""
    llm = rt.revl_model_hop(
        model="m", tokens_in=1, tokens_out=1, cost=None, latency_seconds=0.1,
        attempts=1, attempt_ceiling=3, verified_by=["G9"],
        prompt_digest=rt.revl_prompt_digest(["a secret user prompt string"],
                                            arg_origins=set(), taint_engaged=True))
    spans = ot.build_spans([_model_event(7, "A#a1", llm)])
    hop = next(s for s in spans if s.kind == wr.EMIT)
    keys = set(hop.attributes)
    assert "gen_ai.prompt" not in keys and "gen_ai.completion" not in keys
    # the digest rides only as the salted hash + bucket
    assert hop.attributes["revl.llm.prompt.sha256"].startswith("hmac-sha256:")
    assert hop.attributes["revl.llm.prompt.bytes_bucket"] == "0-64"
    # and the raw prompt text appears in no attribute value
    for v in hop.attributes.values():
        assert "a secret user prompt string" != v


def test_otel_spanlink_only_when_produced_seq_present():
    """The `model-produced` SpanLink appears ONLY when `producedSeq` is present.
    Absent producedSeq -> NO such link (the edge is never adjacency-guessed and
    never exported as a hard proven cause, §4 attack 3)."""
    with_edge = rt.revl_model_hop(
        model="m", tokens_in=1, tokens_out=1, cost=None, latency_seconds=0.1,
        attempts=1, attempt_ceiling=3, verified_by=["G9"], produced_seq=[9])
    without = rt.revl_model_hop(
        model="m", tokens_in=1, tokens_out=1, cost=None, latency_seconds=0.1,
        attempts=1, attempt_ceiling=3, verified_by=["G9"])

    spans_with = ot.build_spans([_model_event(7, "A#a1", with_edge)])
    hop_with = next(s for s in spans_with if s.kind == wr.EMIT)
    produced_links = [l for l in hop_with.links
                      if l.attributes.get("revl.link.relation") == "model-produced"]
    assert len(produced_links) == 1 and produced_links[0].target == "s9"

    spans_without = ot.build_spans([_model_event(7, "A#a1", without)])
    hop_without = next(s for s in spans_without if s.kind == wr.EMIT)
    assert not any(l.attributes.get("revl.link.relation") == "model-produced"
                   for l in hop_without.links)


# ---------------------------------------------------------------------------
# 6. the reader: projection, sort, filter, and the CLI (always exits 0)
# ---------------------------------------------------------------------------


def _trace_with_produced_flow():
    """A single-activation validated model->tool flow, as recorded events: a
    model completion at seq 7 that produced the fs emit at seq 9."""
    llm = rt.revl_model_hop(
        model="openai:gpt-4o", tokens_in=1204, tokens_out=88,
        cost={"amount": 0.0121, "currency": "USD"}, latency_seconds=1.84,
        attempts=1, attempt_ceiling=3, verified_by=["G9"], produced_seq=[9],
        prompt_digest=rt.revl_prompt_digest(["ctx"], arg_origins=set(),
                                            taint_engaged=True))
    return [
        wr.make_event(0, 1, wr.LOAD, "AgentLoop", "PENDING -> ACTIVE",
                      wr.cause_boot(), ts=0.0),
        _model_event(7, "AgentLoop#g1#a1", llm),
        wr.make_emit_event(9, 1, "AgentLoop", "fs", "Report.write",
                           wr.cause_trigger("model produced tool calls"),
                           ts=9.0),
    ]


def test_reader_projects_and_resolves_the_produced_edge():
    doc = tr.compute_trace(_trace_with_produced_flow())
    assert doc["events"] == 3
    assert len(doc["modelHops"]) == 1
    hop = doc["modelHops"][0]
    assert hop["seq"] == 7
    assert hop["model"]["provenance"] == "host-reported"
    assert hop["attempts"] == {"count": 1, "ceiling": 3,
                               "provenance": "revl-controlled"}
    assert hop["produced"] == [{"seq": 9, "capability": "fs",
                                "key": "Report.write"}]
    assert hop["promptDigest"]["salted"].startswith("hmac-sha256:")


def test_reader_component_filter():
    events = _trace_with_produced_flow()
    other = rt.revl_model_hop(
        model="m", tokens_in=1, tokens_out=1, cost=None, latency_seconds=0.1,
        attempts=1, attempt_ceiling=3, verified_by=[])
    events.append(wr.make_emit_event(11, 1, "OtherComp", "model", "M.c",
                                     wr.cause_boot(), llm=other))
    doc = tr.compute_trace(events)
    assert len(doc["modelHops"]) == 2
    only = tr.filter_document(doc, component="OtherComp")
    assert [h["component"] for h in only["modelHops"]] == ["OtherComp"]


def test_reader_render_is_text_and_marks_provenance():
    doc = tr.compute_trace(_trace_with_produced_flow())
    out = tr.render(doc)
    assert "1 model hop(s)" in out
    assert "host-reported - unverified" in out
    assert "revl-measured bracket" in out
    assert "fiber token matched" in out
    # NO raw prompt/response text in the human view
    assert "salted digest - no text captured" in out


def test_cli_trace_exits_zero_and_emits_json(tmp_path):
    path = tmp_path / "run.jsonl"
    wr.write_trace(_trace_with_produced_flow(), str(path))
    env = {"PYTHONPATH": str(ROOT / "src")}
    import os
    env = {**os.environ, **env}
    res = subprocess.run(
        [sys.executable, "-m", "revl", "trace", str(path), "--json"],
        capture_output=True, text=True, env=env)
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["modelHops"][0]["seq"] == 7
    # human view too
    res2 = subprocess.run(
        [sys.executable, "-m", "revl", "trace", str(path)],
        capture_output=True, text=True, env=env)
    assert res2.returncode == 0, res2.stderr
    assert "model hop(s)" in res2.stdout


# ---------------------------------------------------------------------------
# 7. the driver call-site glue (run.py): a LIVE model crossing carries the llm
#    payload; a NON-model crossing is byte-identical to the pre-glue shape.
#    This is item 121's final integration hop — the mechanism (runtime.py) is
#    exercised through the driver's real `_record_emit` / `_model_crossing_payload`.
# ---------------------------------------------------------------------------


def _bare_driver(generation: int = 3):
    """A `_Driver` holding only the trace-recording state the emit glue touches,
    wired to the REAL runtime module — no cordis load needed (the glue reads the
    fiber-local model-call register, which is pure-python contextvars)."""
    driver = run._Driver.__new__(run._Driver)
    driver.runtime = rt
    driver._events = []
    driver._seq = 0
    driver.generation = generation
    return driver


def test_live_model_crossing_records_the_llm_payload_end_to_end():
    """Drive an ACTUAL model crossing through the item-257 `validate_retry` seam
    (a fake host model callback), then record the crossing through the driver's
    real emit glue. The `emit` record must carry the `llm` payload: the
    revl-owned attempt/latency numbers, the host-reported usage passed through,
    a salted digest with a coarse bucket, and the fiber-token-gated produced
    edge — with NO prompt/response TEXT anywhere."""
    # the fake host model: fails validation twice (a bare string is not an
    # object), then returns a well-formed completion carrying host usage.
    calls = {"n": 0}

    def host_model():
        calls["n"] += 1
        if calls["n"] < 3:
            return "not-a-dict"          # fails the object schema -> a retry
        return {"tag": "ok", "model": "openai:gpt-4o-2024-08-06",
                "tokensIn": 1204, "tokensOut": 88,
                "cost": {"amount": 0.0121, "currency": "USD"}}

    validated = rt.validate_retry(host_model, budget=2,
                                  schema={"type": "object"}, where="Agent")
    assert validated["tag"] == "ok"

    driver = _bare_driver()
    activation_id = f"AgentLoop#g{driver.generation}"
    # the single-activation validated flow: the seam mints the value-flow token
    # tied to the completion this fiber just validated (seq 7 below).
    rt.revl_note_validated_completion(activation_id, 7)

    llm = driver._model_crossing_payload(
        activation_id=activation_id,
        args=["system prompt", "user question"],   # revl-typed args, proven clean
        arg_origins={"model", "input"}, taint_engaged=True)
    driver._record_emit("AgentLoop", "model", "Model.complete",
                        wr.cause_trigger("decision point"),
                        llm=llm, activation_id=activation_id)

    ev = driver._events[-1]
    assert ev["event"] == wr.EMIT and ev["seq"] == 0  # first record on this driver
    assert ev["activationId"] == activation_id
    got = ev["llm"]
    # revl-owned numbers: 3 attempts against the static N+1 = 3 ceiling
    assert got["attempts"] == 3 and got["attemptCeiling"] == 3
    assert got["attemptsProvenance"] == "revl-controlled"
    assert got["latencySeconds"] >= 0.0
    assert got["latencyProvenance"] == "revl-measured-bracket"
    # host-reported usage passed through, tagged unverifiable
    assert got["model"] == "openai:gpt-4o-2024-08-06"
    assert got["tokensIn"] == 1204 and got["tokensOut"] == 88
    assert got["modelProvenance"] == "host-reported"
    assert got["usageProvenance"] == "host-reported"
    assert got["cost"]["amount"] == 0.0121
    assert got["cost"]["provenance"] == "host-reported"
    # the salted digest: hash + coarse bucket present, NEVER the raw text
    assert got["promptDigest"]["salted"].startswith("hmac-sha256:")
    assert got["promptDigest"]["bytesBucket"] == "0-64"
    # the fiber-token-gated produced edge (the activation id matched)
    assert got["producedSeq"] == [7]
    # no prompt/response TEXT anywhere in the serialized record
    blob = json.dumps(ev)
    assert "system prompt" not in blob and "user question" not in blob
    assert "prompt" not in got and "response" not in got  # only promptDigest

    # the reader projects the hop, and the OTel span carries the produced link
    doc = tr.compute_trace(list(driver._events))
    hop = doc["modelHops"][0]
    assert hop["attempts"] == {"count": 3, "ceiling": 3,
                               "provenance": "revl-controlled"}
    assert hop["promptDigest"]["salted"].startswith("hmac-sha256:")
    span = next(s for s in ot.build_spans(list(driver._events))
                if s.kind == wr.EMIT)
    assert any(l.attributes.get("revl.link.relation") == "model-produced"
               for l in span.links)
    # the register is consumed: a later non-model crossing inherits nothing
    assert driver._model_crossing_payload(activation_id=activation_id,
                                          args=["later"]) is None


def test_produced_seq_omitted_live_when_activation_id_differs():
    """A model crossing whose activation id does NOT match the fiber's token
    still records the hop, but OMITS `producedSeq` (honest-degrade, never an
    adjacency guess) — the crossing carries the llm payload without the edge."""
    rt.validate_retry(lambda: {"tag": "ok"}, budget=0,
                      schema={"type": "object"}, where="Agent")
    rt.revl_note_validated_completion("AgentLoop#g3#a1", 7)   # a1's token
    driver = _bare_driver()
    llm = driver._model_crossing_payload(activation_id="AgentLoop#g3#a2")  # a2
    assert llm is not None
    assert "producedSeq" not in llm       # omitted: activation ids differ
    assert llm["attempts"] == 1 and llm["attemptCeiling"] == 1


def test_non_model_crossing_record_is_byte_identical_to_pre_glue():
    """A NON-model crossing: no `validate_retry` ran in this fiber, so the
    register is empty and `_model_crossing_payload` returns None. The recorded
    event must be byte-identical (modulo the monotonic `ts`) to what the
    pre-glue `_record_emit` produced — no `llm`, no `activationId`."""
    driver = _bare_driver()
    llm = driver._model_crossing_payload(activation_id="Reporter#g3",
                                         args=["a path"])
    assert llm is None
    cause = wr.cause_trigger("a filesystem write crossed")
    driver._record_emit("Reporter", "fs", "Report.write", cause,
                        llm=llm, activation_id=None)
    ev = driver._events[-1]
    assert "llm" not in ev and "activationId" not in ev
    assert set(ev) == {"v", "seq", "gen", "event", "component", "cause",
                       "capability", "key", "ts"}
    # byte-identical to the pre-121 emit shape (drop the monotonic ts on both)
    expect = wr.make_emit_event(0, 3, "Reporter", "fs", "Report.write", cause)
    assert {k: v for k, v in ev.items() if k != "ts"} == expect


# ---------------------------------------------------------------------------
# 5. the driver actually performs the reset it documents (item 416d)
# ---------------------------------------------------------------------------


def _emit_module_driver(generation: int = 0):
    """A `_Driver` holding only what `_emit_module`'s reset arm reads."""
    driver = run._Driver.__new__(run._Driver)
    driver.runtime = rt
    driver.generation = generation
    return driver


def test_a_new_generation_resets_the_run_trace_state():
    """Item 416d: `revl_reset_run_trace_state` documents "called at run start"
    and, until this landed, ONLY tests called it. So a model completion observed
    in generation N and never consumed by a crossing was still stashed when a
    `--watch` reload booted generation N+1, and the FIRST emit crossing of the
    new program consumed it and was recorded as a model hop it never made.

    Driven through `_emit_module`'s reset arm (the one place a generation
    begins) rather than by calling the runtime seam directly, so the test fails
    if the wiring is removed."""
    # gen N leaves an unconsumed completion in the fiber-local register.
    rt.validate_retry(lambda: {"tag": "ok"}, budget=0,
                      schema={"type": "object"}, where="Agent")
    assert rt._revl_last_model_call.get() is not None

    driver = _emit_module_driver()
    reset = getattr(driver.runtime, "revl_reset_run_trace_state", None)
    assert reset is not None, "the driver's reset seam must exist"
    reset()

    # gen N+1 starts clean: the stale observation cannot be mis-attributed.
    assert rt._revl_last_model_call.get() is None
    assert _bare_driver()._model_crossing_payload(activation_id="C#g2") is None


def test_the_reset_is_wired_into_the_generation_boundary():
    """The reset must be performed BY `_emit_module`, not merely available."""
    import inspect
    src = inspect.getsource(run._Driver._emit_module)
    assert "revl_reset_run_trace_state" in src
    # and it must run before the emit, so gen N+1's own crossings see the new
    # nonce rather than gen N's.
    assert src.index("revl_reset_run_trace_state") < src.index("self.emit.emit")


def test_each_generation_gets_its_own_digest_salt():
    """Two generations of one `--watch` process must not be correlatable by
    digest equality: the nonce is per generation, not per process."""
    args = ["a", "b"]
    first = rt.revl_prompt_digest(args, arg_origins=set(), taint_engaged=True)
    driver = _emit_module_driver()
    driver.runtime.revl_reset_run_trace_state()
    second = rt.revl_prompt_digest(args, arg_origins=set(), taint_engaged=True)
    assert first["salted"] != second["salted"]

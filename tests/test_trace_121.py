"""`revl trace` — the model hop as a first-class span (roadmap item 121,
docs/design/121-revl-trace.md), Slices 1 and 2.

Every mechanism the REVISED design (adversarial review 2026-09-01, plus the
Slice 2 implementation revision) makes non-negotiable is pinned here on
hand-built traces, direct runtime calls and a real recorded timeline (no live
cordis run needed), so the fixtures cannot drift from the real record shape:

* the widened `emit` record carries an `llm` payload + `activationId` (additive);
  a NON-model emit carries neither and is byte-identical to a pre-121 v2 emit;
* `producedSeq` is PROVED, never guessed: the emitter must see the downstream
  emission's arguments read the completion's binding, the fiber-local token must
  say which execution of that completion site produced the value, and the driver
  must resolve it inside one activation. It points FORWARD at the emission the
  hop produced (never a self-loop), and is OMITTED on every unproven path;
* the `promptDigest` is a SALTED HMAC (not the raw sha256) with a COARSE bucket
  (not an exact length); a secret/confidential arg SUPPRESSES it (no digest, hop
  still recorded) and NEVER refuses; a `Secret[T]`-receiving model op still
  compiles; the gate FAILS CLOSED when taint is disengaged;
* the OTel export flattens `llm` onto the span, carries NO prompt/response text,
  and adds a `model-produced` SpanLink ONLY when `producedSeq` is present.
"""

from __future__ import annotations

import asyncio
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

import replay as rp  # noqa: E402
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
# 3. producedSeq (Slice 2): the value-flow token, its identity bridge, and the
#    static fact that gates it. The claim `produced` makes — "the model said
#    this and THAT crossing happened because of it" — is a conjunction of two
#    halves that live on opposite sides of the runtime/driver boundary:
#
#      * the EMITTER's static fact: this crossing's arguments read the binding
#        that validated-completion SITE produced (nothing at runtime can see it);
#      * the RUNTIME's dynamic fact: which execution of that site, identified by
#        the completion crossing's `replay.Step.index`, produced the value.
#
#    The driver joins them by mapping step index -> trace seq. Either half
#    missing OMITS the edge; neither is ever guessed from adjacency.
# ---------------------------------------------------------------------------


def test_the_token_holds_the_completion_crossing_step_index():
    """The mint site can supply neither a driver-owned trace seq nor an
    activation id — `_Driver._seq` is private to run.py and the activation id is
    synthesised at step-back time. `Step.index` is the one identity that already
    crosses, so the token holds THAT."""
    rt.revl_note_emission_index("Agent", 4)              # the recorder recorded step 4
    rt.revl_note_validated_completion("Agent.go#c1")
    assert rt.revl_produced_by("Agent.go#c1") == 4


def test_the_token_is_keyed_by_completion_site_not_by_recency():
    """Two completions live in one body: "the last validated completion in this
    fiber" would attribute the wrong one, so the register is keyed by the
    EMITTER's static site id."""
    rt.revl_note_emission_index("Agent", 4)
    rt.revl_note_validated_completion("Agent.go#c1")
    rt.revl_note_emission_index("Agent", 9)
    rt.revl_note_validated_completion("Agent.go#c2")
    assert rt.revl_produced_by("Agent.go#c1") == 4     # not clobbered by c2
    assert rt.revl_produced_by("Agent.go#c2") == 9


def test_no_token_without_a_recorded_crossing():
    """Recording off: no step index exists, so nothing is minted (rather than a
    bogus index being invented) and the edge honest-degrades to absent."""
    rt.revl_note_validated_completion("Agent.go#c1")
    assert rt.revl_produced_by("Agent.go#c1") is None
    assert rt.revl_produced_by(None) is None


def test_an_unanalysed_call_site_mints_nothing():
    """`site` is optional at the seam: a call site the emitter's flow analysis
    did not reach passes none, and no token is minted for it."""
    rt.revl_note_emission_index("Agent", 4)
    rt.validate_retry(lambda: {"tag": "ok"}, budget=0,
                      schema={"type": "object"}, where="Agent")
    assert rt._revl_validated_completions.get() in (None, {})


def test_the_seam_mints_the_token_for_its_site():
    """Driven through the REAL item-257 seam: a validated response names the
    crossing the recorder just published."""
    rt.revl_note_emission_index("Agent", 11)
    rt.validate_retry(lambda: {"tag": "ok"}, budget=0,
                      schema={"type": "object"}, where="Agent",
                      site="Agent.go#c1")
    assert rt.revl_produced_by("Agent.go#c1") == 11


def test_the_marker_is_consumed_by_exactly_one_crossing():
    """`revl_produced_emit` marks ONE crossing. The recorder consumes the
    marker, so a later crossing in the same fiber inherits nothing."""
    rt.revl_note_emission_index("Agent", 4)
    rt.revl_note_validated_completion("Agent.go#c1")
    seen = []

    def crossing(*args):
        seen.append(rt.revl_take_produced_by())

    rt.revl_produced_emit("Agent.go#c1", crossing, "an arg")
    crossing("a later, unmarked arg")
    assert seen == [4, None]
    assert rt.revl_take_produced_by() is None      # and nothing is left behind


def test_the_marker_is_not_set_without_a_token():
    """No token for the site (that completion has not validated in this fiber):
    the crossing fires unmarked rather than carrying a fabricated edge."""
    seen = []
    rt.revl_produced_emit("Agent.go#c1",
                          lambda: seen.append(rt.revl_take_produced_by()))
    assert seen == [None]


def test_arguments_are_evaluated_before_the_marker_is_set():
    """A crossing NESTED in a marked crossing's arguments records BEFORE the
    marker exists, so it cannot consume the edge meant for the outer one."""
    rt.revl_note_emission_index("Agent", 4)
    rt.revl_note_validated_completion("Agent.go#c1")
    order = []

    def nested():
        order.append(("nested", rt.revl_take_produced_by()))
        return "value"

    rt.revl_produced_emit(
        "Agent.go#c1",
        lambda arg: order.append(("outer", rt.revl_take_produced_by())),
        nested())
    assert order == [("nested", None), ("outer", 4)]


def test_the_token_is_isolated_across_fibers():
    """The register is fiber-local: a completion validated in one asyncio Task
    does not leak its token to a SIBLING task (each gets its own copied
    context), so two live activations cannot cross-attribute."""
    import asyncio

    async def fiber(site, index, other):
        rt.revl_note_emission_index("Loop", index)
        rt.revl_note_validated_completion(site)
        assert rt.revl_produced_by(site) == index
        assert rt.revl_produced_by(other) is None

    async def main():
        await asyncio.gather(
            fiber("Loop.go#c1", 7, "Loop.go#c2"),
            fiber("Loop.go#c2", 11, "Loop.go#c1"),
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


def _replay_module():
    """The backwards-replay recorder (`backends/python/replay.py`), which owns
    `Step.index` — the identity the item-121 value-flow token holds."""
    import replay

    return replay


def test_live_model_crossing_records_the_llm_payload_end_to_end():
    """Drive an ACTUAL model crossing through the item-257 `validate_retry` seam
    (a fake host model callback), then record the crossing through the driver's
    real emit glue. The `emit` record must carry the `llm` payload: the
    revl-owned attempt/latency numbers, the host-reported usage passed through,
    and a salted digest with a coarse bucket — with NO prompt/response TEXT
    anywhere."""
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
    activation_id = f"AgentLoop#g{driver.generation}#a1"
    llm = driver._model_crossing_payload(
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
    # the hop is minted with NO produced edge: the emission it names has not
    # crossed yet, so its seq does not exist. The edge is back-patched later.
    assert "producedSeq" not in got
    # no prompt/response TEXT anywhere in the serialized record
    blob = json.dumps(ev)
    assert "system prompt" not in blob and "user question" not in blob
    assert "prompt" not in got and "response" not in got  # only promptDigest

    # the reader projects the hop, and the register is consumed: a later
    # non-model crossing inherits nothing
    doc = tr.compute_trace(list(driver._events))
    hop = doc["modelHops"][0]
    assert hop["attempts"] == {"count": 3, "ceiling": 3,
                               "provenance": "revl-controlled"}
    assert hop["promptDigest"]["salted"].startswith("hmac-sha256:")
    assert "produced" not in hop
    assert driver._model_crossing_payload(args=["later"]) is None


def _crossing(index, *, produced_by=None, service="fs", label="Report.write"):
    """One `emissionsCrossed` entry as `replay.Step.as_dict()` renders it."""
    detail = {"key": "k", "method": "m", "service": service, "args": ["a"]}
    if produced_by is not None:
        detail["producedBy"] = produced_by
    return {"index": index, "kind": "emission", "label": label, "detail": detail}


def test_the_produced_edge_points_forward_at_the_emission_it_produced():
    """The back-patch, end to end at the driver: the model hop's `producedSeq`
    names the DOWNSTREAM emission's trace seq, resolved from the `producedBy`
    step index the recorder stamped. Never a self-loop — the runtime token is a
    BACKWARD pointer and the reader wants a forward one, so the driver is the
    only place that can turn it around."""
    driver = _bare_driver()
    activation = "AgentLoop#g3#a1"
    rt.validate_retry(lambda: {"tag": "ok"}, budget=0,
                      schema={"type": "object"}, where="Agent")
    hop = driver._model_crossing_payload(args=["p"])
    driver._record_emit("AgentLoop", "model", "Model.complete",
                        wr.cause_trigger("decision point"),
                        llm=hop, activation_id=activation)
    completion = driver._events[-1]
    driver._record_emit("AgentLoop", "fs", "Report.write",
                        wr.cause_trigger("the tool ran"), llm=None,
                        activation_id=None)
    tool = driver._events[-1]

    assert driver._link_produced(tool, completion, activation) is True
    assert completion["llm"]["producedSeq"] == [tool["seq"]]
    assert tool["seq"] != completion["seq"]        # not a self-loop
    # and the reader resolves the edge onto the emission it names
    doc = tr.compute_trace(list(driver._events))
    assert doc["modelHops"][0]["produced"] == [
        {"seq": tool["seq"], "capability": "fs", "key": "Report.write"}]


def test_the_produced_edge_is_omitted_outside_the_activation():
    """The driver-side activation check: a `producedBy` that resolves to no hop
    in the activation being recorded (a different activation of the component,
    or a crossing this step-back never walked) draws NO edge."""
    driver = _bare_driver()
    rt.validate_retry(lambda: {"tag": "ok"}, budget=0,
                      schema={"type": "object"}, where="Agent")
    hop = driver._model_crossing_payload(args=["p"])
    driver._record_emit("AgentLoop", "model", "Model.complete",
                        wr.cause_trigger("decision point"),
                        llm=hop, activation_id="AgentLoop#g3#a1")
    completion = driver._events[-1]
    driver._record_emit("AgentLoop", "fs", "Report.write",
                        wr.cause_trigger("the tool ran"))
    tool = driver._events[-1]

    # unresolvable step index -> nothing to link
    assert driver._link_produced(tool, None, "AgentLoop#g3#a1") is False
    # resolvable, but minted under a DIFFERENT activation of the component
    assert driver._link_produced(tool, completion, "AgentLoop#g3#a2") is False
    assert "producedSeq" not in completion["llm"]
    # a non-model crossing carries no hop to patch
    assert driver._link_produced(tool, tool, None) is False


def test_the_step_back_arm_builds_the_bridge_and_back_patches():
    """The bridge must be built BY the `emissionsCrossed` arm, not merely be
    available: it maps `Step.index` -> the recorded event and resolves every
    `producedBy` against that map. Resolution is deferred to after the walk
    because crossings are reported NEWEST FIRST, so the hop a crossing names is
    recorded after it."""
    import inspect
    src = inspect.getsource(run._Driver._replay)
    arm = src[src.index("emissionsCrossed"):]
    assert "producedBy" in arm and "_link_produced" in arm
    # the map is keyed on the recorder's step index, the identity bridge
    assert 'step["index"]' in arm


def test_the_activation_id_carries_a_per_activation_discriminator():
    """`component + gen` cannot separate two concurrent activations of one
    component (`gen` is a process-global RELOAD counter), so the id the driver
    stamps carries the recorded activation's own ordinal."""
    import inspect
    src = inspect.getsource(run._Driver._replay)
    assert "#a{getattr(timeline, 'activation', 0)}" in src

    replay = _replay_module()
    first = replay.Timeline("Agent")
    second = replay.Timeline("Agent")
    assert first.activation != second.activation


def test_non_model_crossing_record_is_byte_identical_to_pre_glue():
    """A NON-model crossing: no `validate_retry` ran in this fiber, so the
    register is empty and `_model_crossing_payload` returns None. The recorded
    event must be byte-identical (modulo the monotonic `ts`) to what the
    pre-glue `_record_emit` produced — no `llm`, no `activationId`."""
    driver = _bare_driver()
    llm = driver._model_crossing_payload(args=["a path"])
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
# 7b. item 242: the hop is bound to the crossing it was MEASURED at.
#
#     The step-back walk that records crossings reports them NEWEST FIRST
#     (`replay.Timeline.step_back` iterates `reversed(tail)`). While the model-hop
#     discriminator was "did this fiber observe a completion", whichever crossing
#     the walk reached first consumed it — so a run that crossed a completion and
#     then wrote a file attributed the model, token, cost and latency numbers to
#     the write. These pin the record-time marking that replaces it.
# ---------------------------------------------------------------------------


def _model_host_return(model="openai:gpt-4o-2024-08-06", tokens_in=1204):
    return {"tag": "ok", "model": model, "tokensIn": tokens_in, "tokensOut": 88,
            "cost": {"amount": 0.0121, "currency": "USD"}}


def _two_crossing_timeline():
    """One activation that crosses the MODEL boundary and THEN writes a file.

    Both crossings go through the real `Timeline.record_emission`, and the model
    one is recorded from INSIDE `validate_retry`'s `make_call` — the real
    nesting, which is what lets the seam bind its observation to that step."""
    timeline = rp.Timeline("AgentLoop")

    def host_model():
        timeline.record_emission("Model", "complete", ("system", "ask"),
                                 "model", ("agent.rvl", 12))
        return _model_host_return()

    rt.validate_retry(host_model, budget=0, schema={"type": "object"},
                      where="AgentLoop")
    # the LATER, non-model crossing — the one the newest-first walk reaches first
    timeline.record_emission("Report", "write", ("/tmp/out.txt",), "fs",
                             ("agent.rvl", 19))
    return timeline


def _record_the_crossings(driver, timeline, report):
    """The driver's own `emissionsCrossed` arm, over a real step-back report."""
    activation_id = f"{timeline.component}#g{driver.generation}"
    for step in report["emissionsCrossed"]:
        detail = step.get("detail") or {}
        llm = driver._model_crossing_payload(
            args=detail.get("args"),
            crossing=(timeline.component, step.get("index")))
        driver._record_emit(
            timeline.component, detail.get("service") or "",
            step.get("label") or "",
            wr.cause_trigger("crossed by step-back (an emission has no inverse)"),
            llm=llm, activation_id=(activation_id if llm is not None else None))
    return {e["key"]: e for e in driver._events}


def test_the_model_hop_lands_on_the_completion_not_the_newest_crossing():
    """THE REGRESSION. A model completion crossed, then a filesystem write. The
    step-back reports the write FIRST; the `llm` payload must still land on the
    completion, and the write's record must stay byte-identical to a pre-121 v2
    emit. Drop the crossing key from `_model_crossing_payload` and the write
    consumes the completion's numbers, which is the whole defect."""
    timeline = _two_crossing_timeline()
    report = asyncio.run(timeline.step_back(-1, force=True))
    # the ordering the defect fed on, asserted rather than assumed
    assert [s["label"] for s in report["emissionsCrossed"]] == [
        "Report.write", "Model.complete"]

    events = _record_the_crossings(_bare_driver(), timeline, report)

    hop = events["Model.complete"]["llm"]
    assert hop["model"] == "openai:gpt-4o-2024-08-06"
    assert hop["tokensIn"] == 1204 and hop["tokensOut"] == 88
    assert hop["attempts"] == 1 and hop["attemptCeiling"] == 1
    assert hop["cost"]["amount"] == 0.0121
    # the filesystem write is NOT a model hop and carries none of it
    write = events["Report.write"]
    assert "llm" not in write and "activationId" not in write
    assert "openai:gpt-4o-2024-08-06" not in json.dumps(write)


def test_two_completions_in_one_body_are_told_apart_by_their_crossings():
    """Fiber-locality isolates concurrent ACTIVATIONS; it cannot separate two
    completions inside ONE body, which is the lesson `producedSeq` already
    learned from its activation-id key. The crossing identity is the equivalent
    here: two completions are two crossings, so they are two entries, and each
    keeps its own numbers however the walk orders them."""
    timeline = rp.Timeline("AgentLoop")

    def complete(model, tokens_in):
        def host_model():
            timeline.record_emission("Model", "complete", ("ask",), "model",
                                     ("agent.rvl", 12))
            return _model_host_return(model, tokens_in)

        rt.validate_retry(host_model, budget=0, schema={"type": "object"},
                          where="AgentLoop")
        return timeline.steps[-1].index

    first = complete("openai:gpt-4o-2024-08-06", 100)
    second = complete("anthropic:claude-sonnet-4", 200)

    driver = _bare_driver()
    # read them back NEWEST FIRST, the order the walk uses
    newer = driver._model_crossing_payload(crossing=("AgentLoop", second))
    older = driver._model_crossing_payload(crossing=("AgentLoop", first))
    assert newer["model"] == "anthropic:claude-sonnet-4" and newer["tokensIn"] == 200
    assert older["model"] == "openai:gpt-4o-2024-08-06" and older["tokensIn"] == 100
    # and nothing is left over for a third crossing to inherit
    assert driver._model_crossing_payload(crossing=("AgentLoop", 99)) is None


def test_a_completion_never_crosses_a_component_boundary():
    """The key carries the COMPONENT as well as the step index: two timelines
    both number their steps from zero, so the index alone would let one
    component's crossing claim another's hop."""
    timeline = rp.Timeline("AgentLoop")

    def host_model():
        timeline.record_emission("Model", "complete", ("ask",), "model",
                                 ("agent.rvl", 12))
        return _model_host_return()

    rt.validate_retry(host_model, budget=0, schema={"type": "object"},
                      where="AgentLoop")
    index = timeline.steps[-1].index
    driver = _bare_driver()
    assert driver._model_crossing_payload(crossing=("Reporter", index)) is None
    assert driver._model_crossing_payload(crossing=("AgentLoop", index)) is not None


def test_the_recorder_marks_the_crossing_at_record_time():
    """The mark is published BY `record_emission`, and published IN TIME: the
    completion the `validate_retry` seam measures around that record binds to
    THAT step, so a later crossing cannot claim it.

    This used to open with `assert "_note_emission_index" in src` over
    `record_emission`'s text. The name occurring in the method body certifies
    nothing about whether the mark is in place when the observation arrives,
    which is the whole item-242 fix — and the assertions below, plus
    `test_the_model_hop_lands_on_the_completion_not_the_newest_crossing`, red on
    the call being removed anyway. A source grep beside a behavioural assertion
    is the one that gets updated when it breaks."""
    timeline = rp.Timeline("AgentLoop")

    def host_model():
        timeline.record_emission("Model", "complete", ("ask",), "model",
                                 ("agent.rvl", 12))
        return _model_host_return()

    rt.validate_retry(host_model, budget=0, schema={"type": "object"},
                      where="AgentLoop")
    model_step = timeline.steps[-1].index
    assert rt.revl_recorded_crossing() == ("AgentLoop", model_step)

    # a LATER crossing moves the mark on, and does not inherit the completion
    timeline.record_emission("Report", "write", ("/tmp/out.txt",), "fs",
                             ("agent.rvl", 19))
    later = timeline.steps[-1].index
    assert later != model_step
    assert rt.revl_recorded_crossing() == ("AgentLoop", later)

    driver = _bare_driver()
    assert driver._model_crossing_payload(crossing=("AgentLoop", later)) is None
    assert driver._model_crossing_payload(
        crossing=("AgentLoop", model_step)) is not None


def test_the_crossing_key_is_wired_into_the_emissions_arm():
    """The key must be fed BY the `emissionsCrossed` arm, not merely accepted by
    `_model_crossing_payload` — this is what fails if run.py goes back to the
    unkeyed take."""
    import inspect
    src = inspect.getsource(run._Driver)
    arm = src[src.index('for step in report["emissionsCrossed"]'):]
    assert 'crossing=(timeline.component, step.get("index"))' in arm


# ---------------------------------------------------------------------------
# 5. the driver actually performs the reset it documents (item 416d)
# ---------------------------------------------------------------------------


class _StubEmitter:
    """The one `emit` surface `_Driver._emit_module` uses — `emit(ir)` returning
    python source — plus a note of what the per-run trace state looked like AT
    EMIT TIME.

    That note is how the ORDER the item-416d arm documents ("reset before the
    emit, so gen N+1's own crossings see the new nonce") is asserted as a fact
    about the run rather than as a fact about run.py's line numbers. Stubbing
    the emitter is also what keeps the arm drivable with no cordis: the emitted
    module is trivial, so `exec` needs no runtime."""

    def __init__(self) -> None:
        self.calls_at_emit: list = []
        self.crossing_at_emit: list = []

    def emit(self, ir: dict) -> str:
        self.calls_at_emit.append(rt._revl_model_calls.get())
        self.crossing_at_emit.append(rt._revl_recorded_crossing.get())
        return "REVL_EMITTED = True\n"


def _emit_module_driver(generation: int = 0):
    """A `_Driver` holding only what `_emit_module` reads on a bare IR."""
    driver = run._Driver.__new__(run._Driver)
    driver.runtime = rt
    driver.generation = generation
    driver.emit = _StubEmitter()
    driver.root_dirs = []
    driver.config = {}
    driver.secrets = None
    driver.recorder = None
    driver.wal_path = None
    return driver


def test_a_new_generation_resets_the_run_trace_state():
    """Item 416d: `revl_reset_run_trace_state` documents "called at run start"
    and, until this landed, ONLY tests called it. So a model completion observed
    in generation N and never consumed by a crossing was still stashed when a
    `--watch` reload booted generation N+1, and the FIRST emit crossing of the
    new program consumed it and was recorded as a model hop it never made.

    Driven through `_Driver._emit_module` — the one place a generation begins —
    and asserted on the state a generation actually boots into.

    This replaces `test_the_reset_is_wired_into_the_generation_boundary`, which
    asserted `"revl_reset_run_trace_state" in src` plus a source-INDEX ordering
    against `self.emit.emit`. Both were spelling: the seam can be named and
    guarded off (`reset = getattr(...)` against a runtime that does not carry
    it), or called on a value that is not the live runtime, with the grep and
    the index comparison both green and gen N's stale observation still there
    for gen N+1's first crossing to inherit. The ordering is asserted here as a
    fact about the run — the emitter records what the trace state looked like
    when it was called."""
    # gen N leaves an unconsumed completion AND a crossing mark in the
    # fiber-local store — exactly the residue a `--watch` reload used to carry
    # across the boundary.
    rt.revl_note_emission_index("Agent", 5)
    rt.validate_retry(lambda: {"tag": "ok"}, budget=0,
                      schema={"type": "object"}, where="Agent")
    assert rt._revl_model_calls.get() != ()
    assert rt._revl_recorded_crossing.get() == ("Agent", 5)

    driver = _emit_module_driver()
    module = driver._emit_module({"components": [], "manifest": {}})
    assert module.REVL_EMITTED is True

    # the ORDER: gen N's registers were already clear when the emit ran, so
    # gen N+1's own crossings see the new nonce and never gen N's leftovers.
    assert driver.emit.calls_at_emit == [()]
    assert driver.emit.crossing_at_emit == [None]

    # and gen N+1 starts clean: the stale observation cannot be mis-attributed.
    assert rt._revl_model_calls.get() == ()
    assert rt._revl_recorded_crossing.get() is None
    assert rt._revl_validated_completions.get() is None
    assert _bare_driver()._model_crossing_payload() is None


def test_each_generation_gets_its_own_digest_salt():
    """Two generations of one `--watch` process must not be correlatable by
    digest equality: the nonce is per generation, not per process — and the
    re-salt rides the same generation boundary, so this goes through
    `_emit_module` too rather than calling the seam by hand."""
    args = ["a", "b"]
    first = rt.revl_prompt_digest(args, arg_origins=set(), taint_engaged=True)
    _emit_module_driver()._emit_module({"components": [], "manifest": {}})
    second = rt.revl_prompt_digest(args, arg_origins=set(), taint_engaged=True)
    assert first["salted"] != second["salted"]


# ---------------------------------------------------------------------------
# 9. item 444: the compile-to-runtime taint-origin channel.
#
#    Until this landed the driver passed `taint_engaged=False` unconditionally
#    (run.py, the `emissionsCrossed` arm), so the digest was suppressed on EVERY
#    shipped run and the whole surface was inert. The gate now reads the
#    checker's own verdict off the IR the driver already holds — and still fails
#    closed on every path it cannot prove.
# ---------------------------------------------------------------------------

# a composition that declares NO confidentiality surface: a web-tainted value
# crosses a model emission, so the checker records a real origin (`web`) as
# reaching the crossing, and neither `secret` nor `confidential` can exist.
_CLEAN_MODEL_SRC = (
    "extern emission[web.fetch] fn fetch() -> Untrusted[Str] "
    "= @py { return \"x\" }\n"
    "service Model { emission fn complete(p: Str) -> Str }\n"
    "component Agent requires m: Model {\n"
    "  emit m.complete(fetch())\n"
    "}\n")

# the same shape, but the program binds a provider key to `model.complete`
# (item 256 Slice 1). That mints the `secret` origin, so nothing in this
# composition is proven clean and every crossing must stay suppressed.
_SECRET_MODEL_SRC = (
    "secret openai_key for model.complete\n"
    "extern emission[model.complete] fn complete(p: Str) -> Str "
    "= @py { return p }\n"
    "service Model { emission fn ask(p: Str) -> Str }\n"
    "component Agent requires m: Model {\n"
    "  emit m.ask(\"hello\")\n"
    "}\n")


def _driver_for(src: str, filename: str):
    """A `_Driver` holding a REAL compiled IR plus the trace-recording state the
    emit glue touches — the whole compile-to-runtime channel, minus the cordis
    load (the glue reads pure-python contextvars)."""
    driver = _bare_driver(generation=1)
    driver.ir = compile_source(src, filename)
    return driver


def _cross(driver, args):
    """Drive one model completion through the item-257 seam and record the
    crossing exactly as `run.py`'s `emissionsCrossed` arm does."""
    rt.validate_retry(lambda: {"tag": "ok"}, budget=0,
                      schema={"type": "object"}, where="Agent")
    arg_origins, taint_engaged = driver._crossing_taint("Agent")
    return driver._model_crossing_payload(
        args=args, arg_origins=arg_origins, taint_engaged=taint_engaged)


def test_clean_composition_certifies_and_carries_the_checker_origins():
    """The channel's two halves, read straight off a real compiled IR: the
    whole-program certificate, and the checker's recorded crossing origins."""
    from revl import taint

    index = taint.OriginIndex(compile_source(_CLEAN_MODEL_SRC, "clean.rvl"))
    assert index.engaged is True
    # the checker's own verdict (item 249 Decision 5), not a driver guess
    assert index.origins_for("Agent") == frozenset({"web"})
    # a component the walk recorded nothing for carries no origin (not None)
    assert index.origins_for("Nobody") == frozenset()


def test_end_to_end_clean_model_arg_emits_a_within_run_stable_digest():
    """A run whose model arg is PROVEN clean emits a digest, and the same prompt
    twice within one run digests identically — the within-run equality the
    surface exists to provide, and which no shipped run could ever observe while
    `taint_engaged` was hard-wired False."""
    driver = _driver_for(_CLEAN_MODEL_SRC, "clean.rvl")

    first = _cross(driver, ["summarise this", "context"])
    second = _cross(driver, ["summarise this", "context"])
    other = _cross(driver, ["a different prompt"])

    assert first["promptDigest"]["salted"].startswith("hmac-sha256:")
    assert first["promptDigest"]["provenance"] == "revl-side-args"
    # the same prompt twice in ONE run: equal
    assert second["promptDigest"] == first["promptDigest"]
    # a different prompt: a different digest, and no raw text anywhere
    assert other["promptDigest"]["salted"] != first["promptDigest"]["salted"]
    blob = json.dumps([first, second, other])
    assert "summarise this" not in blob and "a different prompt" not in blob


def test_end_to_end_secret_tainted_arg_suppresses_the_digest():
    """The other half of the exit test: a composition that binds a provider key
    mints the `secret` origin, so it is not certified and every crossing's
    digest is suppressed. The hop is still recorded in full — suppression never
    refuses and never drops the crossing (§4, HIGH 2)."""
    driver = _driver_for(_SECRET_MODEL_SRC, "secret.rvl")
    assert driver._crossing_taint("Agent") == (None, False)

    llm = _cross(driver, ["summarise this", "context"])
    assert llm is not None                      # the hop IS recorded
    assert "promptDigest" not in llm            # ...with the digest suppressed
    assert llm["attempts"] == 1 and llm["attemptCeiling"] == 1


@pytest.mark.parametrize("src", [
    # a `Secret[T]` extern return (item 256 Slice 3, §7a)
    "extern emission[payment.charge] fn charge(a: Str) -> Secret[Str] "
    "= @py { return a }\n"
    "service Ops { emission fn go(u: Str) -> Int }\n"
    "component Agent provides ops: Ops {\n  provide ops {\n"
    "    fn go(u) {\n      let t = charge(u)\n      return 0\n    }\n  }\n}\n",
    # a `Secret[T]` extern parameter (a declared disclosure receiver, §7b)
    "extern emission[model.complete] fn prompt(p: Secret[Str]) -> Str "
    "= @py { return \"\" }\n"
    "service Ops { emission fn go(u: Str) -> Int }\n"
    "component Agent provides ops: Ops {\n  provide ops {\n"
    "    fn go(u) {\n      return 0\n    }\n  }\n}\n",
    # a `Secret[T]` service-operation parameter
    "service Ops { emission fn go(u: Secret[Str]) -> Int }\n"
    "component Agent provides ops: Ops {\n  provide ops {\n"
    "    fn go(u) {\n      return 0\n    }\n  }\n}\n",
    # a `Secret[T]` config field
    "service Ops { emission fn go(u: Str) -> Int }\n"
    "component Agent provides ops: Ops {\n  config { key: Secret[Str] }\n"
    "  provide ops {\n    fn go(u) {\n      return 0\n    }\n  }\n}\n",
    # a `Secret[T]` parameter on a top-level fn — the marking that reached NO
    # IR key at all before item 444 closed the gap in `lower.py`
    "fn hold(x: Secret[Str]) -> Int { return 1 }\n"
    "service Ops { emission fn go(u: Str) -> Int }\n"
    "component Agent provides ops: Ops {\n  provide ops {\n"
    "    fn go(u) {\n      let n = hold(u)\n      return 0\n    }\n  }\n}\n",
])
def test_any_declared_confidentiality_surface_certifies_false(src):
    """The certificate is whole-program on purpose: ANY declaration that can
    mint `secret`/`confidential` anywhere closes the gate for the whole
    composition. Over-suppression is the safe direction; a per-crossing
    judgement would rest on an under-approximation across call boundaries."""
    from revl import taint

    ir = compile_source(src, "surface.rvl")
    assert taint.declares_confidential_surface(ir) is True
    assert taint.OriginIndex(ir).engaged is False


def test_a_secret_fn_parameter_now_rides_the_ir():
    """`extract_and_normalize` strips the qualifier off the declared type before
    lowering, so `params[i]["secret"]` is the only surviving record that a
    top-level fn parameter was declared `Secret[T]`. Additive: a fn with no
    qualifier is byte-identical."""
    ir = compile_source(
        "fn hold(x: Secret[Str], y: Str) -> Int { return 1 }\n"
        "service Ops { emission fn go(u: Str) -> Int }\n"
        "component Agent provides ops: Ops {\n  provide ops {\n"
        "    fn go(u) {\n      let n = hold(u, u)\n      return 0\n    }\n  }\n}\n",
        "fnsecret.rvl")
    params = ir["functions"][0]["params"]
    assert params[0] == {"name": "x", "type": "Str", "secret": True}
    assert params[1] == {"name": "y", "type": "Str"}   # no marking, unchanged


def test_the_gate_fails_closed_on_every_unproven_path():
    """A fail-closed gate must stay closed wherever the channel proves nothing:
    a driver with no IR at all, and an IR shape the index cannot read."""
    from revl import taint

    bare = _bare_driver()                       # no `ir` attribute at all
    assert bare._crossing_taint("Agent") == (None, False)
    assert _cross(bare, ["anything"]).get("promptDigest") is None

    for unusable in (None, {}, {"services": {}}, "not-an-ir"):
        index = taint.OriginIndex(unusable)
        assert index.engaged is False
        assert index.origins_for("Agent") is None


class _StubTimeline:
    """The one `Timeline` surface `_replay("back")` touches: a component name and
    a step-back report carrying a single model-completion crossing."""

    component = "Agent"

    def __init__(self, args):
        self._args = args

    async def step_back(self, at, force=False):
        return {
            "inversesRan": [], "compensationsRan": [], "failed": [],
            "emissionsCrossed": [{
                "kind": "emission", "label": "Model.complete",
                "index": _CROSSING_STEP,
                "detail": {"service": "model", "args": self._args},
            }],
            "guarantee": "a crossed emission has no inverse",
        }


class _StubRecorder:
    def __init__(self, args):
        self._timeline = _StubTimeline(args)
        self.timelines = {"Agent": self._timeline}

    def timeline(self, component=None):
        return self._timeline


_CROSSING_STEP = 7


def _step_back_record(src: str, filename: str, args):
    """Drive the SHIPPED `emissionsCrossed` arm — `_Driver._replay("back")`
    itself, not a re-implementation of it — over one model-completion crossing,
    and return the `emit` record it appended.

    This is the wiring assertion with teeth: a driver that stops consulting
    `_crossing_taint`, or feeds the gate anything it cannot prove, changes the
    RECORD, which is what a consumer of `revl trace` actually reads."""
    driver = _driver_for(src, filename)
    driver.recorder = _StubRecorder(args)
    driver._log = lambda *a, **k: None
    driver._flush = lambda: asyncio.sleep(0)
    # the item-242 seam: bind the completion observation to the crossing the
    # arm is about to record, exactly as `record_emission` does at record time.
    rt.revl_note_emission_index("Agent", _CROSSING_STEP)
    rt.validate_retry(lambda: {"tag": "ok"}, budget=0,
                      schema={"type": "object"}, where="Agent")
    asyncio.run(driver._replay("back", 0, False, "Agent"))
    return driver._events[-1]


def test_the_channel_is_wired_into_the_crossing_record():
    """The gate must be fed BY the `emissionsCrossed` arm, not merely be
    available — driven through the arm and asserted on the record it emits.

    Deliberately NOT a source grep. The assertion this replaced matched
    `"taint_engaged=taint_engaged"` in run.py's text, and text certifies
    nothing: un-wiring the arm one line higher (`arg_origins, taint_engaged =
    None, False`) kills the digest on every shipped run and leaves that grep
    green. Both halves of the item-444 exit test are asserted here on the real
    record — a certified-clean composition digests, a `secret`-minting one is
    suppressed with the hop intact."""
    first = _step_back_record(_CLEAN_MODEL_SRC, "clean.rvl",
                              ["summarise this", "context"])
    again = _step_back_record(_CLEAN_MODEL_SRC, "clean.rvl",
                              ["summarise this", "context"])
    other = _step_back_record(_CLEAN_MODEL_SRC, "clean.rvl",
                              ["a different prompt"])

    # `activationId` carries a generation and, since item 121 slice 2, an
    # activation suffix (`Agent#g1#a0`). This test is item 444's -- it owns the
    # taint channel, not the id's shape -- so it pins the generation prefix and
    # leaves the suffix to slice 2's own tests. An exact match here went red the
    # moment the two landed together, each PR green on its own.
    assert first["event"] == wr.EMIT
    assert first["activationId"].startswith("Agent#g1")
    digest = first["llm"]["promptDigest"]
    assert digest["salted"].startswith("hmac-sha256:")
    assert digest["provenance"] == "revl-side-args"
    # the within-run equality the surface exists to provide, observed on the
    # record a real step-back produces
    assert again["llm"]["promptDigest"] == digest
    assert other["llm"]["promptDigest"]["salted"] != digest["salted"]
    blob = json.dumps([first, again, other])
    assert "summarise this" not in blob and "a different prompt" not in blob


def test_a_secret_composition_is_suppressed_in_the_crossing_record():
    """The fail-closed half through the same arm: the hop is recorded in full,
    only the digest is absent (§4, HIGH 2 — suppression never refuses)."""
    ev = _step_back_record(_SECRET_MODEL_SRC, "secret.rvl",
                           ["summarise this", "context"])
    assert ev["llm"] is not None
    assert "promptDigest" not in ev["llm"]
    assert ev["llm"]["attempts"] == 1 and ev["llm"]["attemptCeiling"] == 1


def test_a_new_generation_rebuilds_the_origin_index():
    """`--watch` replaces `self.ir` in place; the index is keyed on the
    document's identity, so generation N+1's certificate is its own."""
    driver = _driver_for(_CLEAN_MODEL_SRC, "clean.rvl")
    assert driver._crossing_taint("Agent")[1] is True
    driver.ir = compile_source(_SECRET_MODEL_SRC, "secret.rvl")
    assert driver._crossing_taint("Agent") == (None, False)


# ---------------------------------------------------------------------------
# 10. Slice 2: the two halves of the `produced` proof at their own seams — the
#     emitter's static value-flow fact, and the recorder's stamp of it.
# ---------------------------------------------------------------------------

_FLOW_PRELUDE = """
type Call = { tool: Str, args: Str }
type AgentTurn = Final(Str) | ToolCalls(List[Call])
service Model { emission[model] validated retry 2 fn complete(h: List[Str]) -> AgentTurn }
service Tool { emission[fs] fn run(s: Str) -> Int }
service Loop { emission fn go(p: Str) -> Int }
"""


def _emit_flow(body: str) -> str:
    import emit as py_emit

    src = (_FLOW_PRELUDE + """
component Agent requires model: Model, tool: Tool provides agent: Loop {
  provide agent {
    fn go(p) {
""" + body + """
    }
  }
}
""")
    return py_emit.emit(compile_source(src, "flow.rvl"))


def test_the_completion_site_rides_the_seam():
    """The emitter names each `validated ... retry` completion crossing with a
    stable static site id and passes it to the seam, so the runtime can key the
    token by SITE rather than by recency."""
    code = _emit_flow("""      let t = emit model.complete(["p"])
      return 1""")
    assert "_revl_validate_retry(lambda: _revl_ctx.model.complete(['p'])" in code
    assert "'Agent.go#c1')" in code


def test_a_crossing_reading_the_completions_binding_is_marked():
    """The static value-flow fact: the downstream crossing's ARGUMENTS read the
    completion's binding, so it fires through the marker helper naming the site
    it derives from. This is the fact only the emitter can see."""
    code = _emit_flow("""      let t = emit model.complete(["p"])
      let n = match t { Final(a) => emit tool.run(a), ToolCalls(c) => 0 }
      return n""")
    assert "_revl_produced_emit('Agent.go#c1', _revl_ctx.tool.run, a)" in code


def test_a_crossing_that_does_not_read_the_binding_is_not_marked():
    """A later crossing whose args are unrelated to the completion is NOT
    marked: fiber adjacency is not a value-flow fact (§4 attack 3), and its
    emission stays byte-identical."""
    code = _emit_flow("""      let t = emit model.complete(["p"])
      let n = emit tool.run("unrelated")
      return n""")
    assert "_revl_ctx.tool.run('unrelated')" in code
    assert "_revl_produced_emit" not in code


def test_a_reassigned_name_is_no_longer_provably_derived():
    """The analysis is a MUST-derive under-approximation: a binding that was
    overwritten cannot be claimed, so the crossing reading it is unmarked and
    the edge is simply absent. A missing edge is a gap in the trace; a wrong one
    is exported to a third-party backend as a proven cause."""
    code = _emit_flow("""      let t = emit model.complete(["p"])
      let s = match t { Final(a) => a, ToolCalls(c) => "other" }
      s = "reassigned"
      let n = emit tool.run(s)
      return n""")
    assert "_revl_ctx.tool.run(s)" in code
    assert "_revl_produced_emit" not in code


def test_a_crossing_reading_two_completions_is_not_attributed_to_either():
    """Arguments that read TWO completions' bindings leave the driver to pick,
    which is the guess this design forbids: no mark, no edge."""
    code = _emit_flow("""      let t = emit model.complete(["p"])
      let u = emit model.complete(["q"])
      let a = match t { Final(x) => x, ToolCalls(c) => "0" }
      let b = match u { Final(y) => y, ToolCalls(d) => "1" }
      let n = emit tool.run(a + b)
      return n""")
    assert "_revl_produced_emit" not in code


def test_a_body_with_no_validated_completion_is_byte_identical():
    """Nothing about the analysis touches a body with no validated-retry
    completion in it."""
    code = _emit_flow("""      let n = emit tool.run("plain")
      return n""")
    assert "_revl_produced_emit" not in code
    assert "produced_emit" not in code       # not even the import


def test_the_recorder_stamps_produced_by_on_the_marked_crossing():
    """The recorder owns `Step.index`: it publishes each crossing's index for
    the seam, and stamps `producedBy` on the crossing the emitter marked."""
    replay = _replay_module()
    timeline = replay.Timeline("Agent")

    completion = timeline.record_emission("model", "complete", (["p"],),
                                          "Model", ("<f>", 1))
    assert rt._revl_last_emission_index.get() == completion.index
    rt.revl_note_validated_completion("Agent.go#c1")

    stamped = []
    rt.revl_produced_emit(
        "Agent.go#c1",
        lambda arg: stamped.append(timeline.record_emission(
            "tool", "run", (arg,), "Tool", ("<f>", 2))),
        "derived")
    tool = stamped[0]
    assert tool.detail["producedBy"] == completion.index
    # and it rides the step-back report the driver reads
    assert replay._emission_entry(tool)["producedBy"] == completion.index
    assert "producedBy" not in replay._emission_entry(completion)


def test_an_unmarked_crossing_records_no_produced_by():
    """No marker, no stamp — the recorded step is byte-identical to a pre-Slice-2
    crossing."""
    replay = _replay_module()
    timeline = replay.Timeline("Agent")
    timeline.record_emission("model", "complete", (["p"],), "Model", ("<f>", 1))
    rt.revl_note_validated_completion("Agent.go#c1")
    plain = timeline.record_emission("tool", "run", ("x",), "Tool", ("<f>", 2))
    assert "producedBy" not in plain.detail
    assert "producedBy" not in replay._emission_entry(plain)


def test_the_marked_crossing_keeps_the_emitted_modules_own_site():
    """The marker helper declares its frame transparent, so the recorded SITE is
    still the emitted module's line — the compensation-adjacency rule and
    `_emission_sites` both key on it."""
    replay = _replay_module()
    timeline = replay.Timeline("Agent")
    timeline.record_emission("model", "complete", (["p"],), "Model", ("<f>", 1))
    rt.revl_note_validated_completion("Agent.go#c1")

    proxy = replay._ServiceProxy(_FakeTool(), timeline, "tool", "Tool", {"run"})
    here = sys._getframe().f_code.co_filename
    rt.revl_produced_emit("Agent.go#c1", proxy.run, "derived")
    marked = timeline.steps[-1]
    assert marked.file == here          # not runtime.py
    assert marked.detail["producedBy"] == 0


class _FakeTool:
    def run(self, arg):
        return arg


class _FakeModel:
    def complete(self, ctx):
        return {"tag": "ok"}


def test_the_bridge_survives_the_newest_first_crossing_report():
    """The whole bridge over a REAL recorded timeline: the seam mints the token
    from the completion crossing's `Step.index`, the marked downstream crossing
    carries `producedBy`, and the driver resolves it to a trace seq.

    `step_back` reports crossings NEWEST FIRST, so the hop a crossing names is
    recorded AFTER it — which is why resolution is deferred to a second pass
    rather than done inline.
    """
    import asyncio

    replay = _replay_module()
    timeline = replay.Timeline("Agent")
    model = replay._ServiceProxy(_FakeModel(), timeline, "model", "Model",
                                 {"complete"})
    tool = replay._ServiceProxy(_FakeTool(), timeline, "tool", "Tool", {"run"})

    # forward execution, exactly as the emitted body renders it
    rt.validate_retry(lambda: model.complete(["p"]), 0, {"type": "object"},
                      "Agent.go", None, "Agent.go#c1")
    rt.revl_produced_emit("Agent.go#c1", tool.run, "derived")

    report = asyncio.run(timeline.step_back(-1, force=True))
    crossings = report["emissionsCrossed"]
    assert [c["index"] for c in crossings] == [1, 0]        # newest first
    assert crossings[0]["detail"]["producedBy"] == 0
    assert "producedBy" not in crossings[1]["detail"]

    # the driver arm: record each crossing, map step index -> event, resolve
    driver = _bare_driver()
    activation = f"Agent#g3#a{timeline.activation}"
    by_step: dict = {}
    pending: list = []
    for crossing in crossings:
        detail = crossing["detail"]
        is_hop = detail["method"] == "complete"
        llm = rt.revl_model_hop(
            model="m", tokens_in=1, tokens_out=1, cost=None,
            latency_seconds=0.1, attempts=1, attempt_ceiling=1,
            verified_by=["G9"]) if is_hop else None
        driver._record_emit("Agent", detail["service"], crossing["label"],
                            wr.cause_trigger("crossed by step-back"),
                            llm=llm,
                            activation_id=activation if llm else None)
        by_step[crossing["index"]] = driver._events[-1]
        if detail.get("producedBy") is not None:
            pending.append((driver._events[-1], detail["producedBy"]))
    for crossing, produced_by in pending:
        assert driver._link_produced(crossing, by_step.get(produced_by),
                                     activation) is True

    hop = by_step[0]
    assert hop["llm"]["producedSeq"] == [by_step[1]["seq"]]
    assert by_step[1]["seq"] != hop["seq"]                  # not a self-loop

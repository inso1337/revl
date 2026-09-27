"""Issue #1462: the derived grammar reaches real constrained decoding.

`revl.decode_grammar` states a GBNF grammar and a JSON Schema for every
`validated` crossing. This file pins how each provider adapter from issue #1461
hands one of them to its provider, and what happens on return:

* **OpenAI-compatible** takes the artifact and CLAIMS it: `response_format`
  (`json-schema` mode) or llama.cpp's `grammar` field (`gbnf` mode). The
  runtime then holds the completion to it.
* **Anthropic** sends the wire schema as a forced tool's `input_schema`, and
  **Gemini** as `responseSchema`. Both only approximate, so neither claims; the
  value is validated on return and the gaps are named.
* **llguidance**, when installed, compiles each derived GBNF grammar before an
  adapter claims it, and checks a `gbnf`-claimed completion byte for byte.

Every test talks to a loopback fake server. Nothing reaches a real provider.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))
sys.path.insert(0, str(ROOT / "tests"))

from revl.compiler import compile_files  # noqa: E402
from revl.decode_grammar import decode_grammar_for, json_schema_grammar_for  # noqa: E402
from revl.mcp.schema import json_schema_for  # noqa: E402
from revl.parser import Parser  # noqa: E402
from revl.providers import (  # noqa: E402
    PlacementRefused, ProviderConfigError, build_hosts, describe_hosts,
    parse_config, placement_of_program,
)
from revl.providers import structured as st  # noqa: E402

import runtime as rt  # noqa: E402
from runtime import (  # noqa: E402
    GrammarNotHonouredError, register_grammars, validate_retry,
    validate_retry_async,
)

from test_model_providers_1461 import FakeProvider  # noqa: E402

HAVE_ENGINE = st.engine_available()
needs_engine = pytest.mark.skipif(
    not HAVE_ENGINE, reason="needs llguidance (the `test` extra installs it)")


PROGRAM = """
model role local on_device reaches []
type Call = { tool: Str, args: Str }
type AgentTurn = Final(Str) | ToolCalls(List[Call])
type Scores = { by_name: Map[Str, Int] }
service Model {
  emission[model.local] validated fn turn(prompt: Str) -> AgentTurn
  emission[model.local] validated async fn turn_async(prompt: Str) -> AgentTurn
  emission[model.local] validated fn call(prompt: Str) -> Call
  emission[model.local] validated fn scores(prompt: Str) -> Scores
}
service Loop { emission fn run(p: Str) -> Int }
component Agent requires model: Model provides agent: Loop {
  provide agent {
    fn run(p) {
      let t = emit model.turn(p)
      return 1
    }
  }
}
"""

CANONICAL = '{"tag": "Final", "value": "done"}'
#: schema-valid, inside the wire schema, and outside the GBNF only by one
#: leading space. The decoded value is IDENTICAL to CANONICAL's, so no check
#: on the decoded value can see the difference; only a byte-level one can.
LEADING_SPACE = ' {"tag": "Final", "value": "done"}'
#: valid for item 257's schema (nested objects are open there) and outside
#: the wire schema, which closes them: the differential for a json-schema claim.
EXTRA_MEMBER = ('{"tag": "ToolCalls", "value": [{"tool": "s", "args": "q", '
                '"note": "extra"}]}')


@pytest.fixture
def fake():
    server = FakeProvider(CANONICAL)
    yield server
    server.close()


@pytest.fixture(scope="module")
def ir(tmp_path_factory):
    path = tmp_path_factory.mktemp("p") / "app.rvl"
    path.write_text(PROGRAM)
    return compile_files([str(path)])


@pytest.fixture(autouse=True)
def _registry(ir):
    """Register the program's grammars exactly as the emitted module does:
    the IR's `response_grammar` plus the second dialect. And no claim left over
    from another test."""
    table = {}
    for method, spec in ir["services"]["Model"]["methods"].items():
        entry = dict(spec["response_grammar"])
        entry["wire_schema"] = json_schema_grammar_for(spec["response_schema"])
        table[f"Model.{method}"] = entry
    register_grammars(table)
    rt._revl_grammar_claim.set(None)
    yield
    rt._revl_grammar_claim.set(None)


def _spec(ir, method):
    return ir["services"]["Model"]["methods"][method]


def _host(ir, fake, **entry):
    roles = {"local": {"provider": "openai-compatible",
                       "base_url": fake.base + "/v1", "model": "m", **entry}}
    if entry.get("provider") in ("anthropic", "gemini"):
        roles["local"] = {"model": "m", "base_url": fake.base,
                          "api_key_env": "K1462", "residence": "off_device",
                          **entry}
        # only for these tests: the program's role is on_device, and a hosted
        # API may not serve it, so the fixture program is re-read off_device
        source = PROGRAM.replace("local on_device", "local off_device")
    else:
        source = PROGRAM
    placement = placement_of_program(Parser(source, "app.rvl").parse())
    host = build_hosts(ir, placement, parse_config({"roles": roles}),
                       environ={"K1462": "k-1462-test-value"})["model"]
    host._revl_attach_runtime(rt)
    return host


def _validate(ir, host, method="turn", budget=0):
    spec = _spec(ir, method)
    return validate_retry(lambda: getattr(host, method)("go"), budget,
                          spec["response_schema"], f"Model.{method}", None,
                          grammar=f"Model.{method}")


# --------------------------------------------------------------------------
# OpenAI-compatible: json-schema mode claims response_format
# --------------------------------------------------------------------------

def test_json_schema_mode_sends_the_wire_schema_and_claims_it(ir, fake):
    host = _host(ir, fake)
    assert _validate(ir, host) == json.loads(CANONICAL)
    body = fake.requests[-1]["body"]
    wire = json_schema_grammar_for(_spec(ir, "turn")["response_schema"])
    assert body["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "revl_response", "schema": wire["schema"], "strict": True}}
    assert "grammar" not in body


def test_a_json_schema_claim_is_what_refuses_an_unconstrained_answer(ir, fake):
    """The differential. The same reply, twice: accepted when the adapter sent
    no constraint, refused by name when it claimed one. A server that ignored
    `response_format` is therefore caught rather than believed."""
    fake.reply_text = EXTRA_MEMBER
    plain = _host(ir, fake, structured_output="none")
    assert _validate(ir, plain)["tag"] == "ToolCalls"
    assert "response_format" not in fake.requests[-1]["body"]

    claimed = _host(ir, fake)
    with pytest.raises(GrammarNotHonouredError) as exc:
        _validate(ir, claimed)
    assert "not honoured" in str(exc.value)


def test_no_structured_output_without_the_runtime(ir, fake):
    """A host with no runtime attached has no grammar registry to read and no
    claim seam to use, so it sends nothing rather than guessing."""
    host = _host(ir, fake)
    host._revl_attach_runtime(None)
    host.turn("go")
    assert "response_format" not in fake.requests[-1]["body"]


def test_an_unvalidated_operation_gets_no_constraint(tmp_path, fake):
    source = PROGRAM.replace("validated fn call", "fn call").replace(
        "-> Call\n", "-> Str\n")
    path = tmp_path / "u.rvl"
    path.write_text(source)
    ir = compile_files([str(path)])
    placement = placement_of_program(Parser(source, "u.rvl").parse())
    host = build_hosts(ir, placement, parse_config({"roles": {"local": {
        "provider": "openai-compatible", "base_url": fake.base + "/v1",
        "model": "m"}}}))["model"]
    host._revl_attach_runtime(rt)
    host.call("go")
    assert "response_format" not in fake.requests[-1]["body"]


def test_the_claim_survives_the_async_worker_thread(ir, fake):
    """A claim is a context variable, and the request runs on a worker thread
    in a COPY of the context. The host takes the claim before handing off, so
    `validate_retry_async` still sees it: the refusal below cannot happen
    otherwise."""
    fake.reply_text = EXTRA_MEMBER
    host = _host(ir, fake)
    spec = _spec(ir, "turn_async")

    async def run():
        return await validate_retry_async(
            lambda: host.turn_async("go"), 0, spec["response_schema"],
            "Model.turn_async", None, grammar="Model.turn_async")

    with pytest.raises(GrammarNotHonouredError):
        asyncio.run(run())


def test_a_failed_request_does_not_leave_a_claim_behind(ir, fake):
    host = _host(ir, fake)
    fake.mode = "echo-401"
    with pytest.raises(Exception):
        host.turn("go")
    assert rt._revl_grammar_claim.get() is None


# --------------------------------------------------------------------------
# OpenAI-compatible: gbnf mode, and the local engine
# --------------------------------------------------------------------------

def test_gbnf_mode_sends_the_grammar_and_claims_it(ir, fake):
    host = _host(ir, fake, structured_output="gbnf")
    assert _validate(ir, host) == json.loads(CANONICAL)
    body = fake.requests[-1]["body"]
    assert body["grammar"] == _spec(ir, "turn")["response_grammar"]["text"]
    assert "response_format" not in body


@needs_engine
def test_the_engine_sees_what_the_decoded_value_cannot(ir, fake, monkeypatch):
    """A server that ignores `grammar` (Ollama does, silently: design note 542
    section 11.1) can return text that decodes to exactly the canonical value.
    Only the bytes differ, so only the engine can refuse it, and the paired
    run without the engine shows the runtime's own check does not."""
    fake.reply_text = LEADING_SPACE
    host = _host(ir, fake, structured_output="gbnf")
    with pytest.raises(GrammarNotHonouredError) as exc:
        _validate(ir, host)
    assert "byte for byte" in str(exc.value)
    assert rt._revl_grammar_claim.get() is None

    monkeypatch.setattr(st, "engine_available", lambda: False)
    assert _validate(ir, host) == json.loads(CANONICAL)


@needs_engine
def test_a_byte_level_refusal_is_retried_under_the_budget(ir, fake):
    """The engine's refusal is a response fault like any other, so the
    `retry N` loop re-issues the completion instead of failing the crossing."""
    replies = [LEADING_SPACE, CANONICAL]
    original = fake.reply

    def reply(path, body):
        fake.reply_text = replies.pop(0) if replies else CANONICAL
        return original(path, body)

    fake.reply = reply
    host = _host(ir, fake, structured_output="gbnf")
    assert _validate(ir, host, budget=1) == json.loads(CANONICAL)
    assert len(fake.requests) == 2


@needs_engine
def test_every_derived_grammar_compiles_in_the_engine():
    """Design note 542 section 11.5 left "that the emitted GBNF parses" as
    unverified: the endpoint it was measured on ignored the grammar. A real
    GBNF engine now reads every grammar below, accepts the canonical rendering
    of a value and refuses the member-transposed one."""
    types = {
        "Call": {"kind": "record", "fields": {"tool": "Str", "args": "Str"}},
        "Turn": {"kind": "variant", "cases": [
            {"name": "Final", "payload": "Str"},
            {"name": "Calls", "payload": "List[Call]"},
            {"name": "Stop"}]},
        "Wide": {"kind": "record", "fields": {
            "n": "Int", "x": "Float", "ok": "Bool", "maybe": "Opt[Str]",
            "tags": "List[Str]", "m": "Map[Str, Int]"}},
    }
    cases = {
        "Str": ['"hi"', None],
        "Int": ["-12", None],
        "Opt[Int]": ["null", None],
        "List[Str]": ['["a", "b"]', None],
        "Call": ['{"tool": "s", "args": "q"}', '{"args": "q", "tool": "s"}'],
        "Turn": ['{"tag": "Stop"}', '{"value": "x", "tag": "Final"}'],
        "Wide": ['{"n": 1, "x": 1.5, "ok": true, "maybe": null, "tags": [], '
                 '"m": {"a": 1}}', '{"x": 1.5, "n": 1, "ok": true, '
                 '"maybe": null, "tags": [], "m": {}}'],
    }
    for name, (good, bad) in cases.items():
        grammar = decode_grammar_for(json_schema_for(name, types,
                                                     validated=True))
        recogniser = st.Recogniser(grammar["text"])
        assert recogniser.error(good) is None, (name, good)
        if bad is not None:
            assert recogniser.error(bad) is not None, (name, bad)


def test_a_grammar_the_engine_refuses_is_never_claimed(ir, fake, monkeypatch):
    def refuse(text, digest):
        raise st.GrammarEngineError("llguidance refuses the grammar: test")

    monkeypatch.setattr("revl.providers.host.recogniser_for", refuse)
    with pytest.raises(PlacementRefused, match="refused by the local grammar"):
        _host(ir, fake, structured_output="gbnf")
    # json-schema mode does not depend on the engine
    _host(ir, fake)


# --------------------------------------------------------------------------
# Anthropic: a forced tool, no claim
# --------------------------------------------------------------------------

def _anthropic_tool_reply(value):
    def reply(path, body):
        return {"model": "fake-claude", "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "id": "t1",
                             "name": st.TOOL_NAME, "input": value}],
                "usage": {"input_tokens": 5, "output_tokens": 6}}
    return reply


def test_anthropic_forces_a_tool_and_unwraps_a_non_object_root(ir, fake):
    fake.reply = _anthropic_tool_reply({"value": {"tag": "Final",
                                                  "value": "done"}})
    host = _host(ir, fake, provider="anthropic")
    assert _validate(ir, host) == {"tag": "Final", "value": "done"}
    body = fake.requests[-1]["body"]
    wire = json_schema_grammar_for(_spec(ir, "turn")["response_schema"])
    [tool] = body["tools"]
    assert tool["name"] == st.TOOL_NAME
    assert tool["input_schema"]["properties"]["value"] == wire["schema"]
    assert body["tool_choice"] == {"type": "tool", "name": st.TOOL_NAME}


def test_anthropic_sends_an_object_root_unwrapped(ir, fake):
    fake.reply = _anthropic_tool_reply({"tool": "s", "args": "q"})
    host = _host(ir, fake, provider="anthropic")
    assert _validate(ir, host, method="call") == {"tool": "s", "args": "q"}
    wire = json_schema_grammar_for(_spec(ir, "call")["response_schema"])
    assert fake.requests[-1]["body"]["tools"][0]["input_schema"] == \
        wire["schema"]


def test_anthropic_claims_nothing_so_its_answer_is_validated_only(ir, fake):
    """Tool input is schema-guided, not grammar-constrained, so the adapter
    makes no claim. An answer with an extra nested member is therefore
    accepted exactly as item 257 accepts it, where a json-schema claim would
    have refused it: the approximation is a stated gap, not a false claim."""
    fake.reply = _anthropic_tool_reply({"value": json.loads(EXTRA_MEMBER)})
    host = _host(ir, fake, provider="anthropic")
    assert _validate(ir, host)["tag"] == "ToolCalls"


# --------------------------------------------------------------------------
# Gemini: responseSchema, no claim
# --------------------------------------------------------------------------

def test_gemini_sends_a_translated_response_schema(ir, fake):
    host = _host(ir, fake, provider="gemini")
    assert _validate(ir, host) == json.loads(CANONICAL)
    config = fake.requests[-1]["body"]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    schema = config["responseSchema"]
    final, calls = schema["anyOf"]
    assert final["properties"]["tag"] == {"type": "STRING", "enum": ["Final"]}
    assert final["propertyOrdering"] == ["tag", "value"]
    assert calls["properties"]["value"]["items"]["propertyOrdering"] == \
        ["tool", "args"]
    assert "additionalProperties" not in json.dumps(schema)


def test_gemini_sends_only_the_mime_type_for_a_map(ir, fake):
    host = _host(ir, fake, provider="gemini")
    fake.reply_text = '{"by_name": {"a": 1}}'
    assert _validate(ir, host, method="scores") == {"by_name": {"a": 1}}
    config = fake.requests[-1]["body"]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert "responseSchema" not in config


# --------------------------------------------------------------------------
# what each adapter represents exactly, and what it approximates
# --------------------------------------------------------------------------

def _wire(type_name, types=None):
    return json_schema_grammar_for(
        json_schema_for(type_name, types or {}, validated=True))["schema"]


TYPES = {
    "Call": {"kind": "record", "fields": {"tool": "Str", "args": "Str"}},
    "Turn": {"kind": "variant", "cases": [{"name": "Final", "payload": "Str"},
                                          {"name": "Stop"}]},
}


def test_representation_table():
    """The table docs/model-providers.md prints, pinned."""
    rep = st.representation
    # OpenAI-compatible: exact for an object root; a non-object root or oneOf
    # is exact on local servers and named for hosted strict mode
    assert rep("openai-compatible", "json-schema", _wire("Call", TYPES)) == ()
    assert "non-object root" in rep("openai-compatible", "json-schema",
                                    _wire("Turn", TYPES))[0]
    assert rep("openai-compatible", "gbnf", None) == ()
    # Anthropic: always approximated
    assert rep("anthropic", "tool", _wire("Call", TYPES)) == \
        (st.APPROXIMATE_TOOL,)
    # Gemini: scalars, lists and Opt are exact; a record loses closure; a Map
    # has no form at all
    for exact in ("Str", "Int", "Float", "Bool", "List[Int]", "Opt[Str]"):
        assert rep("gemini", "response-schema", _wire(exact)) == (), exact
    assert "closed object" in rep("gemini", "response-schema",
                                  _wire("Call", TYPES))[0]
    assert "Map[Str, V]" in rep("gemini", "response-schema",
                                _wire("Map[Str, Int]"))[0]


def test_plan_names_the_mode_and_every_gap(ir, fake):
    lines = "\n".join(describe_hosts({"model": _host(ir, fake,
                                                     provider="gemini")}))
    assert "structured output: response-schema (no claim)" in lines
    assert "has no `responseSchema` form" in lines          # scores: a Map
    lines = "\n".join(describe_hosts({"model": _host(ir, fake)}))
    assert "structured output: json-schema (claims the decode)" in lines


@pytest.mark.parametrize("provider,mode", [
    ("openai-compatible", "tool"), ("anthropic", "gbnf"),
    ("gemini", "json-schema")])
def test_a_mode_the_provider_has_not_is_refused(provider, mode):
    entry = {"provider": provider, "model": "m", "structured_output": mode,
             "base_url": "http://127.0.0.1:1/v1"}
    if provider != "openai-compatible":
        entry["api_key_env"] = "K1462"
    with pytest.raises(ProviderConfigError, match="structured_output"):
        parse_config({"roles": {"r": entry}})


# --------------------------------------------------------------------------
# the benchmark tool runs the same path
# --------------------------------------------------------------------------

def test_the_benchmark_measures_through_the_runtime_path(fake, capsys):
    """`bench/structured_output_bench.py` is loaded by path and run against the
    fake server: each arm crosses the real seam, and a server that ignores the
    constraint shows up as a named fault, not as a pass."""
    import importlib.util  # noqa: PLC0415
    spec = importlib.util.spec_from_file_location(
        "revl_bench_structured_output_1462",
        ROOT / "bench" / "structured_output_bench.py")
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)

    fake.reply_text = EXTRA_MEMBER
    assert bench.main(["--base-url", fake.base + "/v1", "--model", "fake",
                       "--n", "1", "--arms", "none,json-schema"]) == 0
    bodies = [r["body"] for r in fake.requests]
    assert "response_format" not in bodies[0]
    assert "response_format" in bodies[1]
    out = capsys.readouterr().out
    assert "| none | 1 | 1 |" in out
    assert "| json-schema | 1 | 0 |" in out


# --------------------------------------------------------------------------
# `revl run --providers`: the runtime is attached, so the seam is live
# --------------------------------------------------------------------------

try:  # noqa: SIM105
    import cordis  # noqa: F401
    HAVE_CORDIS = True
except ModuleNotFoundError:  # pragma: no cover - depends on the interpreter
    HAVE_CORDIS = False


@pytest.mark.skipif(not HAVE_CORDIS, reason="needs the cordis-py runtime")
def test_revl_run_attaches_the_grammar_and_holds_the_answer_to_it(tmp_path,
                                                                  fake):
    """End to end: the emitted module registers the grammars, `revl run`
    attaches the runtime to the model host, the crossing goes out with
    `response_format`, and an answer outside the wire schema is refused by
    name inside the running composition."""
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    app = tmp_path / "app.rvl"
    app.write_text(PROGRAM)
    cfg = tmp_path / "providers.json"
    cfg.write_text(json.dumps({"roles": {"local": {
        "provider": "openai-compatible", "base_url": fake.base + "/v1",
        "model": "m"}}}))
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}

    def run():
        return subprocess.run(
            [sys.executable, "-m", "revl", "run", str(app), "--providers",
             str(cfg)], input='agent.run("go")\n', capture_output=True,
            text=True, env=env, timeout=240, check=False)

    done = run()
    assert done.returncode == 0, done.stderr
    import re  # noqa: PLC0415
    assert re.search(r"call\s+\|\s+=>\s+\|\s+1\s*$", done.stdout, re.M), \
        done.stdout
    assert fake.requests[-1]["body"]["response_format"]["type"] == \
        "json_schema"

    fake.reply_text = EXTRA_MEMBER
    done = run()
    assert re.search(r"error\s+\|\s+GrammarNotHonouredError\|.*stated "
                     r"decoding grammar not honoured", done.stdout), done.stdout

"""Issue #1461: runtime model provider adapters.

Every test here talks to a loopback HTTP server started by the test itself,
one wire format per handler. Nothing reaches a real provider: every
`base_url` below is `127.0.0.1`. The one live test is opt-in
(`REVL_LIVE_OLLAMA_MODEL`) and skips cleanly without a local Ollama.

What is pinned, in the order the issue lists it:

* the three wire formats (OpenAI-compatible, Anthropic Messages, Gemini on
  Google AI and on Vertex): the request each sends and the reply each reads;
* the credential: read from the variable the configuration names, and absent
  from the IR, the manifest, the trace, the log, every error, every repr and
  the `--plan` output, with the server-side half proving it WAS sent;
* the placement: the program's roles, residences and reaches decide which
  adapter a crossing may use, checked before boot, and the adapter never
  chooses;
* `bench/` uses the same client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_files  # noqa: E402
from revl.parser import Parser  # noqa: E402
from revl.providers import (  # noqa: E402
    Adapter, CompletionRequest, PlacementRefused, ProviderConfigError,
    ProviderError, bind_for_run, build_hosts, check_bindings, describe_hosts,
    load_config, model_operations, parse_config, placement_of_program,
    request_json,
)

try:  # noqa: SIM105
    import cordis  # noqa: F401
    HAVE_CORDIS = True
except ModuleNotFoundError:  # pragma: no cover - depends on the interpreter
    HAVE_CORDIS = False

#: The credential every test uses. Distinctive, so a substring search for it
#: cannot match anything by accident.
SECRET = "sk-test-1461-Zq8vR2mXk7PwLc4TnYh9"
ENV = "REVL_TEST_PROVIDER_KEY_1461"


# --------------------------------------------------------------------------
# the fake provider
# --------------------------------------------------------------------------

class FakeProvider:
    """A loopback server that answers in whichever wire format the request
    path names, and records every request it is sent."""

    def __init__(self, reply_text="hello from the fake"):
        self.requests = []
        self.reply_text = reply_text
        self.mode = "ok"            # ok | echo-401 | redirect | not-object
        self.redirect_to = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null")
                outer.requests.append(
                    {"path": self.path,
                     # lower-cased: urllib capitalises the names it sends
                     "headers": {k.lower(): v for k, v in self.headers.items()},
                     "body": body})
                if outer.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to)
                    self.end_headers()
                    return
                if outer.mode == "echo-401":
                    # several real providers echo the rejected key
                    echoed = " ".join(f"{k}={v}" for k, v in self.headers.items())
                    self._send(401, {"error": f"invalid credential: {echoed}"})
                    return
                if outer.mode == "not-object":
                    self._send(200, ["not", "an", "object"])
                    return
                self._send(200, outer.reply(self.path, body))

            def _send(self, code, payload):
                data = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def reply(self, path, body):
        text = self.reply_text
        if path.endswith("/chat/completions"):
            return {"model": "fake-oa", "choices": [{
                "message": {"content": text, "reasoning": "thinking..."},
                "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                          "completion_tokens_details": {"reasoning_tokens": 3}}}
        if path.endswith("/v1/messages"):
            return {"model": "fake-claude", "stop_reason": "end_turn",
                    "content": [{"type": "thinking", "thinking": "hmm"},
                                {"type": "text", "text": text}],
                    "usage": {"input_tokens": 12, "output_tokens": 8}}
        if path.endswith(":generateContent"):
            return {"modelVersion": "fake-gemini",
                    "candidates": [{"finishReason": "STOP", "content": {
                        "parts": [{"text": "plan", "thought": True},
                                  {"text": text}]}}],
                    "usageMetadata": {"promptTokenCount": 13,
                                      "candidatesTokenCount": 9,
                                      "thoughtsTokenCount": 2}}
        return {"error": f"unknown path {path}"}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    server = FakeProvider()
    yield server
    server.close()


@pytest.fixture
def fake2():
    server = FakeProvider("from the second endpoint")
    yield server
    server.close()


def _binding(**entry):
    return parse_config({"roles": {"r": entry}}, "test").binding("r")


def _env():
    return {ENV: SECRET}


# --------------------------------------------------------------------------
# the three wire formats
# --------------------------------------------------------------------------

def test_openai_compatible_request_and_reply(fake):
    b = _binding(provider="openai-compatible", base_url=fake.base + "/v1",
                 model="qwen", api_key_env=ENV)
    c = Adapter(b, environ=_env()).complete(
        CompletionRequest(prompt="hi", system="be brief", top_p=0.5, seed=7))
    req = fake.requests[-1]
    assert req["path"] == "/v1/chat/completions"
    assert req["headers"]["authorization"] == f"Bearer {SECRET}"
    assert req["body"]["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"}]
    assert req["body"]["model"] == "qwen"
    assert req["body"]["top_p"] == 0.5 and req["body"]["seed"] == 7
    assert req["body"]["stream"] is False
    assert c.text == "hello from the fake"
    assert c.reasoning == "thinking..."           # never mixed into the answer
    assert (c.tokens_in, c.tokens_out, c.reasoning_tokens) == (11, 7, 3)
    assert c.finish_reason == "stop" and c.model == "fake-oa"
    assert c.provider == "openai-compatible" and c.latency_seconds >= 0


def test_openai_compatible_sends_no_authorization_without_a_credential(fake):
    b = _binding(provider="openai-compatible", base_url=fake.base + "/v1",
                 model="qwen")
    Adapter(b).complete(CompletionRequest(prompt="hi"))
    assert "authorization" not in fake.requests[-1]["headers"]


def test_anthropic_messages_request_and_reply(fake):
    b = _binding(provider="anthropic", base_url=fake.base, model="claude-x",
                 api_key_env=ENV, max_tokens=64)
    c = Adapter(b, environ=_env()).complete(
        CompletionRequest(prompt="hi", system="sys", seed=3))
    req = fake.requests[-1]
    assert req["path"] == "/v1/messages"
    assert req["headers"]["x-api-key"] == SECRET
    assert req["headers"]["anthropic-version"] == "2023-06-01"
    assert "authorization" not in req["headers"]
    assert req["body"]["system"] == "sys"
    assert req["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert req["body"]["max_tokens"] == 64
    assert "seed" not in req["body"]              # the API has none
    assert c.text == "hello from the fake" and c.reasoning == "hmm"
    assert (c.tokens_in, c.tokens_out) == (12, 8)
    assert c.finish_reason == "end_turn"


def test_gemini_google_ai_request_and_reply(fake):
    b = _binding(provider="gemini", base_url=fake.base + "/v1beta",
                 model="gemini-2.5-flash", api_key_env=ENV)
    c = Adapter(b, environ=_env()).complete(
        CompletionRequest(prompt="hi", system="sys", seed=1))
    req = fake.requests[-1]
    assert req["path"] == "/v1beta/models/gemini-2.5-flash:generateContent"
    assert "key=" not in req["path"]              # never in the URL
    assert req["headers"]["x-goog-api-key"] == SECRET
    assert req["body"]["contents"] == [
        {"role": "user", "parts": [{"text": "hi"}]}]
    assert req["body"]["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert req["body"]["generationConfig"]["seed"] == 1
    assert c.text == "hello from the fake" and c.reasoning == "plan"
    assert (c.tokens_in, c.tokens_out, c.reasoning_tokens) == (13, 9, 2)
    assert c.model == "fake-gemini"


def test_gemini_vertex_request(fake):
    b = _binding(provider="gemini", api="vertex", base_url=fake.base + "/v1",
                 project="my-proj", location="europe-west4",
                 model="gemini-2.5-pro", api_key_env=ENV)
    Adapter(b, environ=_env()).complete(CompletionRequest(prompt="hi"))
    req = fake.requests[-1]
    assert req["path"] == ("/v1/projects/my-proj/locations/europe-west4/"
                           "publishers/google/models/gemini-2.5-pro"
                           ":generateContent")
    assert req["headers"]["authorization"] == f"Bearer {SECRET}"
    assert "x-goog-api-key" not in req["headers"]


def test_vertex_default_base_is_regional():
    b = _binding(provider="gemini", api="vertex", project="p",
                 location="us-central1", model="gemini-2.5-pro",
                 api_key_env=ENV)
    assert b.base_url == "https://us-central1-aiplatform.googleapis.com/v1"


# --------------------------------------------------------------------------
# the transport
# --------------------------------------------------------------------------

def test_a_redirect_is_refused_and_the_other_host_never_contacted(fake, fake2):
    fake.mode = "redirect"
    fake.redirect_to = fake2.base + "/v1/chat/completions"
    b = _binding(provider="openai-compatible", base_url=fake.base + "/v1",
                 model="m")
    with pytest.raises(ProviderError, match="redirect refused") as exc:
        Adapter(b).complete(CompletionRequest(prompt="hi"))
    assert exc.value.status == 302
    assert fake2.requests == []


def test_a_reply_that_is_not_an_object_is_refused(fake):
    fake.mode = "not-object"
    with pytest.raises(ProviderError, match="not an object"):
        request_json(fake.base + "/x", body={})


def test_an_unreachable_endpoint_is_a_named_error():
    with pytest.raises(ProviderError, match="cannot reach"):
        request_json("http://127.0.0.1:1/v1/chat/completions", body={},
                     timeout=5)


# --------------------------------------------------------------------------
# the configuration: what it may not contain
# --------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["api_key", "token", "Authorization"])
def test_a_credential_in_the_file_is_refused_without_echoing_it(field):
    with pytest.raises(ProviderConfigError) as exc:
        parse_config({"roles": {"r": {
            "provider": "anthropic", "model": "m", "api_key_env": ENV,
            field: SECRET}}})
    assert "looks like a credential" in str(exc.value)
    assert SECRET not in str(exc.value)


def test_a_key_pasted_where_the_variable_name_goes_is_refused_unechoed():
    with pytest.raises(ProviderConfigError) as exc:
        parse_config({"roles": {"r": {
            "provider": "anthropic", "model": "m", "api_key_env": SECRET}}})
    assert "NAME of an environment variable" in str(exc.value)
    assert SECRET not in str(exc.value)


@pytest.mark.parametrize("url", [
    f"https://user:{SECRET}@api.example.com",
    f"https://api.example.com/v1?key={SECRET}",
])
def test_a_credential_in_the_url_is_refused_unechoed(url):
    with pytest.raises(ProviderConfigError) as exc:
        parse_config({"roles": {"r": {
            "provider": "openai-compatible", "model": "m", "base_url": url}}})
    assert SECRET not in str(exc.value)


def test_a_credential_over_plain_http_to_another_host_is_refused():
    with pytest.raises(ProviderConfigError, match="in clear"):
        _binding(provider="openai-compatible", model="m",
                 base_url="http://10.0.0.5:8000/v1", api_key_env=ENV)
    # the same over loopback is this machine and is fine
    _binding(provider="openai-compatible", model="m",
             base_url="http://127.0.0.1:8000/v1", api_key_env=ENV)


def test_residence_is_derived_from_the_endpoint():
    local = _binding(provider="openai-compatible", model="m",
                     base_url="http://localhost:11434/v1")
    lan = _binding(provider="openai-compatible", model="m",
                   base_url="http://192.168.1.20:11434/v1")
    v6 = _binding(provider="openai-compatible", model="m",
                  base_url="http://[::1]:8080/v1")
    hosted = _binding(provider="anthropic", model="m", api_key_env=ENV,
                      base_url="http://127.0.0.1:9999")
    assert (local.residence, lan.residence, v6.residence, hosted.residence) \
        == ("on_device", "off_device", "on_device", "off_device")


def test_residence_can_be_narrowed_and_never_widened():
    proxy = _binding(provider="openai-compatible", model="m",
                     base_url="http://127.0.0.1:4000/v1",
                     residence="off_device")
    assert proxy.residence == "off_device"
    with pytest.raises(ProviderConfigError, match="never widened"):
        _binding(provider="openai-compatible", model="m",
                 base_url="http://10.1.1.1/v1", residence="on_device")
    with pytest.raises(ProviderConfigError, match="hosted API"):
        _binding(provider="anthropic", model="m", api_key_env=ENV,
                 residence="on_device")


def test_an_unknown_field_is_refused_not_ignored():
    with pytest.raises(ProviderConfigError, match="unknown field"):
        _binding(provider="openai-compatible", model="m",
                 base_url="http://127.0.0.1/v1", temprature=0.2)
    with pytest.raises(ProviderConfigError, match="provider"):
        _binding(provider="bedrock", model="m")


def test_hosted_apis_need_a_credential_variable():
    with pytest.raises(ProviderConfigError, match="api_key_env"):
        _binding(provider="gemini", model="gemini-2.5-flash")


def test_toml_and_json_load_the_same(tmp_path):
    (tmp_path / "p.toml").write_text(
        '[roles.cloud]\nprovider = "anthropic"\nmodel = "m"\n'
        f'api_key_env = "{ENV}"\n')
    (tmp_path / "p.json").write_text(json.dumps({"roles": {"cloud": {
        "provider": "anthropic", "model": "m", "api_key_env": ENV}}}))
    assert load_config(tmp_path / "p.toml").bindings["cloud"] == \
        load_config(tmp_path / "p.json").bindings["cloud"]


# --------------------------------------------------------------------------
# the placement: the program decides, the configuration is checked
# --------------------------------------------------------------------------

PROGRAM = """
model role local on_device reaches []
model role cloud off_device reaches []

service Model {
  emission[model.local] fn private_label(system: Str, text: Str) -> Str
  emission[model.cloud] fn public_label(text: Str) -> Str
}
service Answer {
  emission fn classify(text: Str) -> Str
  emission fn summarize(doc: Str) -> Str
}

component Classifier requires llm: Model provides out: Answer {
  route model on classify {
    confidential -> local,
    * -> cloud
  }
  route model on summarize { * -> local }
  provide out {
    fn classify(text) {
      let a = emit llm.public_label(text)
      return a
    }
    fn summarize(doc) {
      let a = emit llm.private_label("be brief", doc)
      return a
    }
  }
}
"""


def _compile(tmp_path, source=PROGRAM, name="app.rvl"):
    path = tmp_path / name
    path.write_text(source)
    return path, compile_files([str(path)])


def _placement(source=PROGRAM):
    return placement_of_program(Parser(source, "app.rvl").parse())


def _config(fake, **overrides):
    roles = {
        "local": {"provider": "openai-compatible",
                  "base_url": fake.base + "/v1", "model": "small"},
        "cloud": {"provider": "anthropic", "base_url": fake.base,
                  "model": "claude-x", "api_key_env": ENV},
    }
    for role, entry in overrides.items():
        if entry is None:
            roles.pop(role)
        else:
            roles[role] = entry
    return parse_config({"roles": roles}, "providers.json")


def _refusals(tmp_path, config, source=PROGRAM):
    _, ir = _compile(tmp_path, source)
    placement = _placement(source)
    return check_bindings(placement, config, model_operations(
        ir, placement.roles))


def test_the_program_as_configured_is_admitted(tmp_path, fake):
    assert _refusals(tmp_path, _config(fake)) == []


def test_an_on_device_role_bound_off_the_device_is_refused(tmp_path, fake):
    """The rule that makes the compile-time confidentiality ceiling true at run
    time. The refusal names the route that sends `confidential` to the role."""
    config = _config(fake, local={
        "provider": "anthropic", "base_url": fake.base, "model": "claude-x",
        "api_key_env": ENV})
    refusals = _refusals(tmp_path, config)
    assert len(refusals) == 1
    text = refusals[0].render()
    assert "model role `local` is declared `on_device`" in text
    assert "`route model on classify` in Classifier" in text
    assert "G-MODEL-PLACE" in text


def test_the_same_binding_is_admitted_for_an_off_device_role(tmp_path, fake):
    """Non-vacuity for the rule above: the binding that was refused is
    admitted once the program declares the role off the device (and routes no
    confidential value to it). The program decided, not the adapter."""
    source = PROGRAM.replace("model role local on_device reaches []",
                             "model role local off_device reaches []")
    source = source.replace("confidential -> local,\n", "")
    config = _config(fake, local={
        "provider": "anthropic", "base_url": fake.base, "model": "claude-x",
        "api_key_env": ENV})
    assert _refusals(tmp_path, config, source) == []


def test_a_binding_for_an_undeclared_role_is_refused(tmp_path, fake):
    config = _config(fake, gpu={"provider": "openai-compatible",
                                "base_url": fake.base + "/v1", "model": "m"})
    [refusal] = _refusals(tmp_path, config)
    assert "binds model role `gpu`, which the program does not declare" \
        in refusal.message


def test_a_crossing_on_an_unbound_role_is_refused(tmp_path, fake):
    [refusal] = _refusals(tmp_path, _config(fake, cloud=None))
    assert "`Model.public_label` is placed on model role `cloud`" \
        in refusal.message
    assert "does not bind" in refusal.message


def test_a_crossing_naming_no_role_is_refused(tmp_path, fake):
    """`model.complete` is an operation token, not a placement. Serving it
    would mean the adapter layer picked a role, which it never does."""
    source = PROGRAM.replace("emission[model.cloud] fn public_label",
                             "emission[model.complete] fn public_label")
    refusals = _refusals(tmp_path, _config(fake), source)
    assert any("names no declared model role" in r.message for r in refusals)


def test_a_binding_reaching_past_its_role_is_refused(tmp_path, fake):
    config = _config(fake, local={
        "provider": "openai-compatible", "base_url": fake.base + "/v1",
        "model": "small", "reaches": ["net"]})
    [refusal] = _refusals(tmp_path, config)
    assert "reaches `net`, which the role does not" in refusal.message
    # and the same reach is admitted for a role that declares it. Only the
    # placement is read from the variant: the attenuation fold (item 519)
    # would also ask the component to hold `net`, which is not this check.
    config = _config(fake, cloud={
        "provider": "anthropic", "base_url": fake.base, "model": "claude-x",
        "api_key_env": ENV, "reaches": ["net"]})
    _, ir = _compile(tmp_path)
    wider = _placement(PROGRAM.replace("model role cloud off_device reaches []",
                                       "model role cloud off_device reaches [net]"))
    assert check_bindings(wider, config,
                          model_operations(ir, wider.roles)) == []


def test_an_unvalidated_typed_completion_is_refused(tmp_path, fake):
    source = PROGRAM.replace(
        "emission[model.cloud] fn public_label(text: Str) -> Str",
        "emission[model.cloud] fn public_label(text: Str) -> Int")
    source = source.replace("let a = emit llm.public_label(text)\n      return a",
                            "let a = emit llm.public_label(text)\n      return text")
    refusals = _refusals(tmp_path, _config(fake), source)
    assert any("returns `Int` and is not `validated`" in r.message
               for r in refusals)


# --------------------------------------------------------------------------
# the host: one method per operation, wired to its role's adapter
# --------------------------------------------------------------------------

def _hosts(tmp_path, fake, **overrides):
    _, ir = _compile(tmp_path)
    return build_hosts(ir, _placement(), _config(fake, **overrides),
                       environ=_env())


def test_each_operation_reaches_only_its_roles_adapter(tmp_path, fake, fake2):
    """Two roles, two endpoints: the operation's capability decides which one
    a call reaches, and the other endpoint is never contacted."""
    hosts = _hosts(tmp_path, fake, cloud={
        "provider": "anthropic", "base_url": fake2.base, "model": "claude-x",
        "api_key_env": ENV})
    llm = hosts["llm"]
    assert llm.public_label("x") == "from the second endpoint"
    assert [r["path"] for r in fake2.requests] == ["/v1/messages"]
    assert fake.requests == []
    assert llm.private_label("be brief", "doc") == "hello from the fake"
    assert [r["path"] for r in fake.requests] == ["/v1/chat/completions"]
    assert len(fake2.requests) == 1


def test_a_system_parameter_is_the_system_prompt(tmp_path, fake):
    llm = _hosts(tmp_path, fake)["llm"]
    llm.private_label("be brief", "the doc")
    assert fake.requests[-1]["body"]["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "the doc"}]
    assert llm.last_completion.tokens_out == 7


def test_build_hosts_refuses_what_check_bindings_refuses(tmp_path, fake):
    """However the host is constructed, it runs the placement check first:
    there is no path to a host the program's placement forbids."""
    with pytest.raises(PlacementRefused, match="declared `on_device`"):
        _hosts(tmp_path, fake, local={
            "provider": "gemini", "base_url": fake.base, "model": "g",
            "api_key_env": ENV})


VALIDATED = """
model role local on_device reaches []
type Call = { tool: Str, args: Str }
type AgentTurn = Final(Str) | ToolCalls(List[Call])
service Model {
  emission[model.local] validated fn turn(prompt: Str) -> AgentTurn
  emission[model.local] async fn say(a: Str, b: Int) -> Str
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


def test_a_validated_operation_gets_decoded_json(tmp_path, fake):
    _, ir = _compile(tmp_path, VALIDATED)
    config = parse_config({"roles": {"local": {
        "provider": "openai-compatible", "base_url": fake.base + "/v1",
        "model": "m"}}})
    host = build_hosts(ir, _placement(VALIDATED), config)["model"]
    fake.reply_text = '{"tag": "Final", "value": "done"}'
    assert host.turn("go") == {"tag": "Final", "value": "done"}
    # prose is returned as prose, for item 257's seam to refuse; it is not
    # helped to look valid
    fake.reply_text = "```json\n{}\n```"
    assert host.turn("go") == "```json\n{}\n```"


def test_an_async_operation_is_a_coroutine_and_args_become_json(tmp_path,
                                                                 fake):
    _, ir = _compile(tmp_path, VALIDATED)
    config = parse_config({"roles": {"local": {
        "provider": "openai-compatible", "base_url": fake.base + "/v1",
        "model": "m"}}})
    host = build_hosts(ir, _placement(VALIDATED), config)["model"]
    assert asyncio.run(host.say("x", 2)) == "hello from the fake"
    assert json.loads(fake.requests[-1]["body"]["messages"][-1]["content"]) \
        == {"a": "x", "b": 2}


def test_a_missing_credential_at_dispatch_names_the_variable(tmp_path, fake):
    _, ir = _compile(tmp_path)
    hosts = build_hosts(ir, _placement(), _config(fake), environ={})
    with pytest.raises(ProviderError, match=ENV):
        hosts["llm"].public_label("x")
    assert fake.requests == []          # nothing was sent without it


# --------------------------------------------------------------------------
# the credential never appears anywhere but the request header
# --------------------------------------------------------------------------

def test_the_credential_is_in_no_artifact_message_repr_or_log(tmp_path, fake,
                                                              caplog):
    """The issue's proof obligation, collected in one place. The key is set in
    the environment for the whole test; every artifact and message the
    feature produces is then searched for it. The server-side assertion at the
    end is what makes the search non-vacuous: the key WAS sent."""
    caplog.set_level(logging.DEBUG)
    path, ir = _compile(tmp_path)
    cfg_path = tmp_path / "providers.json"
    cfg_path.write_text(json.dumps({"roles": {
        "local": {"provider": "openai-compatible",
                  "base_url": fake.base + "/v1", "model": "small"},
        "cloud": {"provider": "anthropic", "base_url": fake.base,
                  "model": "claude-x", "api_key_env": ENV}}}))
    env = {**os.environ, ENV: SECRET}
    seen = []

    # IR and manifest
    seen.append(json.dumps(ir))
    seen.append(json.dumps(ir.get("manifest")))
    # the configuration file names the variable, not the value
    seen.append(cfg_path.read_text())
    hosts = bind_for_run(ir, [str(path)], str(cfg_path), environ=env)
    seen.append("\n".join(describe_hosts(hosts)))
    seen.append(repr(hosts) + repr(load_config(cfg_path)))
    for host in hosts.values():
        for _, (_, adapter) in host._revl_routes.items():
            seen.append(repr(adapter) + repr(adapter.binding))
            seen.append(repr(vars(adapter)))

    llm = hosts["llm"]
    llm.public_label("hello")
    seen.append(repr(llm.last_completion))

    # every error path: an echoed 401, a redirect, an unreachable endpoint
    for mode in ("echo-401", "redirect"):
        fake.mode = mode
        fake.redirect_to = f"http://127.0.0.1:1/?k={SECRET}"
        with pytest.raises(ProviderError) as exc:
            llm.public_label("hello")
        seen.append(str(exc.value))
        seen.append(repr(exc.value.__cause__) + repr(exc.value.__context__))
    fake.mode = "ok"
    seen.append(caplog.text)

    for text in seen:
        assert SECRET not in text
    # the echoed 401 really did carry the key back, so the redaction is what
    # kept it out of the message
    assert any(r["headers"].get("x-api-key") == SECRET for r in fake.requests)
    assert "[redacted]" in "".join(seen)


# --------------------------------------------------------------------------
# `revl run --providers`
# --------------------------------------------------------------------------

def _run_cli(args, env_extra=None, input_text=""):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "revl", "run", *args],
                          capture_output=True, text=True, input=input_text,
                          env=env, check=False, timeout=240)


def _write_config(tmp_path, fake, local=None):
    cfg = tmp_path / "providers.json"
    cfg.write_text(json.dumps({"roles": {
        "local": local or {"provider": "openai-compatible",
                           "base_url": fake.base + "/v1", "model": "small"},
        "cloud": {"provider": "anthropic", "base_url": fake.base,
                  "model": "claude-x", "api_key_env": ENV}}}))
    return cfg


def test_run_plan_prints_the_bindings_and_not_the_key(tmp_path, fake):
    path, _ = _compile(tmp_path)
    cfg = _write_config(tmp_path, fake)
    done = _run_cli([str(path), "--plan", "--providers", str(cfg)],
                    {ENV: SECRET})
    assert done.returncode == 0, done.stderr
    assert "llm.public_label -> role cloud: anthropic claude-x" in done.stdout
    assert f"credential from ${ENV}" in done.stdout
    assert SECRET not in done.stdout + done.stderr
    assert fake.requests == []           # checking makes no request


def test_run_refuses_a_forbidden_binding_before_any_runtime(tmp_path, fake):
    path, _ = _compile(tmp_path)
    cfg = _write_config(tmp_path, fake, local={
        "provider": "anthropic", "base_url": fake.base, "model": "c",
        "api_key_env": ENV})
    done = _run_cli([str(path), "--providers", str(cfg)], {ENV: SECRET})
    assert done.returncode == 1
    assert "model role `local` is declared `on_device`" in done.stderr
    assert "== load composition ==" not in done.stdout


def test_run_refuses_an_unset_credential_variable(tmp_path, fake):
    path, _ = _compile(tmp_path)
    cfg = _write_config(tmp_path, fake)
    env = {ENV: ""}
    done = _run_cli([str(path), "--plan", "--providers", str(cfg)], env)
    assert done.returncode == 1
    assert f"`{ENV}` (role `cloud`)" in done.stderr


def test_run_refuses_providers_on_another_tier(tmp_path, fake):
    path, _ = _compile(tmp_path)
    cfg = _write_config(tmp_path, fake)
    done = _run_cli([str(path), "--backend", "ts", "--providers", str(cfg)],
                    {ENV: SECRET})
    assert done.returncode == 1
    assert "py tier only" in done.stderr


@pytest.mark.skipif(not HAVE_CORDIS, reason="needs the cordis-py runtime")
def test_run_serves_a_model_crossing_end_to_end_without_leaking_the_key(
        tmp_path, fake):
    """The whole path: `revl run --providers` boots the composition, the
    component's `emit llm.public_label(...)` reaches the Anthropic adapter
    through the ambient model host, and the answer comes back to the REPL.
    The trace file, stdout and stderr are then searched for the key."""
    path, _ = _compile(tmp_path)
    cfg = _write_config(tmp_path, fake)
    trace = tmp_path / "trace.jsonl"
    done = _run_cli([str(path), "--providers", str(cfg), "--trace",
                     str(trace)], {ENV: SECRET},
                    input_text='out.classify("is this spam")\n')
    assert done.returncode == 0, done.stderr
    assert "'hello from the fake'" in done.stdout
    assert fake.requests[-1]["path"] == "/v1/messages"
    assert fake.requests[-1]["headers"]["x-api-key"] == SECRET
    assert fake.requests[-1]["body"]["messages"][0]["content"] == "is this spam"
    assert trace.exists() and trace.read_text().strip()
    for text in (done.stdout, done.stderr, trace.read_text()):
        assert SECRET not in text


# --------------------------------------------------------------------------
# one client: bench/ uses the adapters
# --------------------------------------------------------------------------

def _load_by_path(name, path):
    import importlib.util  # noqa: PLC0415
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bench_clients_send_through_the_adapter(fake):
    """No `bench/` tool that talks to a model endpoint builds its own urllib
    request any more, and the local runner's request is the adapter's."""
    for name in ("run.py", "model_pin.py", "decode_grammar_probe.py"):
        text = (ROOT / "bench" / name).read_text()
        assert "import urllib" not in text, name
        assert "urllib.request" not in text, name
    bench_run = _load_by_path("bench_run_1461", ROOT / "bench" / "run.py")
    row = bench_run.run_local("sys", "prompt", "m", fake.base + "/v1", 30)
    assert row["text"] == "hello from the fake"
    assert row["output_tokens"] == 7 and row["reasoning_tokens"] == 3
    assert fake.requests[-1]["path"] == "/v1/chat/completions"


def test_bench_run_falls_back_to_the_reasoning_channel(fake):
    bench_run = _load_by_path("bench_run_1461b", ROOT / "bench" / "run.py")
    fake.reply_text = ""
    row = bench_run.run_local("sys", "prompt", "m", fake.base + "/v1", 30)
    assert row["text"] == "thinking..." and row["answer_from_reasoning"]


# --------------------------------------------------------------------------
# opt-in live test against a local Ollama
# --------------------------------------------------------------------------

LIVE_MODEL = os.environ.get("REVL_LIVE_OLLAMA_MODEL")
#: Ollama's default loopback address. Only the model is configurable, so the
#: test cannot be pointed off the device.
LIVE_BASE = "http://127.0.0.1:11434"


def _ollama_up() -> bool:
    try:
        with urllib.request.urlopen(LIVE_BASE + "/api/tags", timeout=2):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not LIVE_MODEL,
                    reason="opt-in: set REVL_LIVE_OLLAMA_MODEL to a pulled "
                           "Ollama model tag")
def test_live_local_ollama_answers_through_the_adapter():
    if not _ollama_up():
        pytest.skip(f"no Ollama answering at {LIVE_BASE}")
    b = _binding(provider="openai-compatible", base_url=LIVE_BASE + "/v1",
                 model=LIVE_MODEL, max_tokens=256, timeout=900)
    assert b.residence == "on_device"
    c = Adapter(b).complete(CompletionRequest(
        prompt="Reply with the single word: ready"))
    assert c.text.strip() or c.reasoning.strip()
    assert c.tokens_out is None or c.tokens_out > 0

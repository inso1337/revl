"""`through a2a` — the A2A 1.0.0 wire binding for a remote provider (item 439).

`docs/design/439-a2a-transport-binding.md`. The `remote` row (item 424 gap (c),
slice C2, `src/revl/synthesize.py`) synthesizes a provider that crosses a
declared seam. It speaks one wire by default — the placement bridge's canonical
envelope, `{"key","method","args"}` -> `{"ok","value"|"error"}` — selected by
OMITTING `through`. This slice binds the first NAMED wire: `through a2a`, which
MAPS that same canonical seam envelope onto A2A 1.0.0's `message/send` at the
boundary.

The mapping, which is the whole of the slice:

  * the one `Str` argument becomes the message's single text `Part`;
  * the method name rides as the `revl.skill` metadata reference;
  * the reply text is read back from a TERMINAL `Task`/`Message`;
  * a transport failure is a FAULT (or an `Err` under `on_failure(result)`),
    the same two settlements the canonical wire already has.

It binds exactly the subset `revl import a2a` binds — text in, text out, one
terminal crossing — so item 439's load-bearing open question (does an A2A Task
map to one emission, to a stream (item 130), or to a session (item 250)?) is
neither answered nor pre-empted here. A non-terminal reply faults; nothing polls
or resumes.

This file is the seam/remote-provider exit test for the binding. It sits beside
`test_424_remote_row.py` (the canonical wire) and `test_import_a2a.py` (the
sibling entry point onto the same protocol).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.composition import compile_composition, resolve_file  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.import_a2a import A2A_VERSION  # noqa: E402
from revl.synthesize import _py_body_a2a  # noqa: E402

# A text-in / text-out service — the shape `through a2a` binds. Not one word of
# a consumer of `agent: Agent` changes between a local provider and this remote
# one (D-424c.1): the A2A-ness is an admission fact on the row, never a wiring
# fact at the call site.
AGENT = """
service Agent {
  emission fn ask(question: Str) -> Str
}
"""

# The same service whose method returns `Result[Str, Str]`, so `on_failure`
# can bring a transport failure back IN BAND (D-424c.3).
AGENT_RESULT = """
service Agent {
  emission fn ask(question: Str) -> Result[Str, Str]
}
"""


def write(tmp_path: Path, **files: str) -> Path:
    for name, text in files.items():
        (tmp_path / f"{name}.rvl").write_text(text)
    return tmp_path


def resolve(tmp_path: Path, doc: str = "base"):
    return resolve_file(str(tmp_path / f"{doc}.rvl"), str(tmp_path))


def _synth_source(tmp_path: Path, base: str, services: str = AGENT) -> str:
    write(tmp_path, services=services, base=base)
    table = resolve(tmp_path)
    (_rel, text), = table.sources.items()
    return text


WITHDRAW = """
composition Net {
  use "services.rvl"
  remote @agent provides agent: Agent
    at host("agent.example:8443")
    through a2a
}
"""


# ---------------------------------------------------- the binding is live

def test_through_a2a_is_bound_and_resolves(tmp_path):
    """`through a2a` no longer refuses (it did until this slice). The row
    resolves, carrying `a2a` as its transport, and synthesizes a provider."""
    write(tmp_path, services=AGENT, base=WITHDRAW)
    table = resolve(tmp_path)
    row, = table.rows
    assert row.remote.get("transport") == "a2a"
    (_rel, text), = table.sources.items()
    assert "component RemoteAgentProvider provides agent: Agent" in text


def test_the_synthesized_a2a_provider_compiles(tmp_path):
    """The synthesized source is ordinary revl, compiled by the ordinary
    compiler: `_link` runs G2/G3/G4 over the `@py` A2A body exactly as over the
    canonical one. This is the soundness argument — nothing here is trusted."""
    write(tmp_path, services=AGENT, base=WITHDRAW)
    document = compile_composition(str(tmp_path / "base.rvl"), str(tmp_path))
    assert document is not None


def test_the_wire_is_a2a_message_send_not_the_canonical_envelope(tmp_path):
    """The mapping: the generated body POSTs an A2A JSON-RPC `message/send`, not
    the canonical `{"key","method","args"}` envelope. The one argument becomes
    the message's text `Part`; the method name rides as `revl.skill`."""
    text = _synth_source(tmp_path, WITHDRAW)
    assert '"method": "message/send"' in text
    assert '"jsonrpc": "2.0"' in text
    assert '"revl.skill": "ask"' in text
    assert '"kind": "text", "text": _message' in text
    assert "_message = _args[0]" in text
    # It is NOT the canonical envelope — that wire is the default (omitted)
    # `through`, and a header that claimed A2A while sending the canonical body
    # is exactly the dishonesty `check_transport` refuses.
    assert '{"key":' not in text
    assert '"args": list(_args)' not in text


def test_the_version_claim_is_exact(tmp_path):
    """Item 439 decision (3): the header claims `A2A 1.0.0`, never bare `A2A`.
    The protocol moves; a binding that followed it silently would assert a
    compatibility nobody checked."""
    text = _synth_source(tmp_path, WITHDRAW)
    assert A2A_VERSION == "1.0.0"
    assert f"A2A {A2A_VERSION} over JSON-RPC 2.0" in text


def test_only_terminal_tasks_are_accepted(tmp_path):
    """Item 439's open question is neither answered nor pre-empted: this wire
    binds only the terminal single crossing. A task still `working` /
    `input-required` / `auth-required` is a lifecycle this slice does not
    express, so the body faults rather than polling or resuming."""
    text = _synth_source(tmp_path, WITHDRAW)
    assert '"completed"' in text and '"failed"' in text
    assert "non-terminal state" in text
    assert "does not poll" in text


def test_the_header_states_the_peer_is_a_claim(tmp_path):
    """Item 439 decision (2): an external agent is not a revl composition, so
    NOTHING about it is checked — it is item 329's untrusted-author case by
    construction. The header says so, in the remote row's own `no verified
    remote badge` language (D-424c.8)."""
    text = _synth_source(tmp_path, WITHDRAW)
    assert "A2A PEER IS A CLAIM" in text
    assert "329" in text


# ------------------------------------------------- the two failure settlements

def test_on_failure_withdraw_raises_a_fault(tmp_path):
    """The default. A transport failure is a FAULT (peer-death withdrawal is
    the settlement, R2/R3), never a quietly-empty result. Item 439 T0: the
    fault is a typed `TransportFault` the activation runtime maps to provider
    withdrawal, carrying the row label and the crossing that failed."""
    text = _synth_source(tmp_path, WITHDRAW)
    assert 'raise TransportFault("a2a: transport failure") from _exc' in text
    assert "class TransportFault(RuntimeError):" in text
    assert "_revl_transport_fault = True" in text
    assert '_revl_row = "agent"' in text
    assert '_revl_crossing = "ask"' in text
    assert "return Err(" not in text


def test_on_failure_result_brings_it_back_in_band(tmp_path):
    """`on_failure(result)`, admitted because the method returns
    `Result[Str, Str]` (D-424c.3): a transport failure and a peer error both
    come back as `Err`, not as a raised fault."""
    base = WITHDRAW.replace("through a2a", "through a2a\n    on_failure(result)")
    text = _synth_source(tmp_path, base, services=AGENT_RESULT)
    assert 'return Err("a2a: transport failure")' in text
    assert 'return Ok(_value)' in text
    assert 'raise RuntimeError("a2a: transport failure")' not in text


# ------------------------------------------------- the redirect policy still holds

def test_a_redirect_is_refused_by_default(tmp_path):
    """The peer authority is the address, and `crossing_redirect` refuses a
    `Location` to another origin — the same policy the canonical wire and the
    importer install. A redirect is NOT a transport failure, so it is raised
    even under `on_failure(result)`."""
    text = _synth_source(tmp_path, WITHDRAW)
    assert "_RedirectRefused" in text
    assert "except _RedirectRefused:" in text


# ------------------------------------------------------- text-in / text-out only

def test_a_multi_parameter_method_is_refused(tmp_path):
    """A2A `message/send` crosses ONE user message. A method with more than one
    parameter has no single text `Part` to become, so it is refused naming the
    method rather than flattened."""
    services = """
service Agent {
  emission fn ask(question: Str, context: Str) -> Str
}
"""
    write(tmp_path, services=services, base=WITHDRAW)
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    message = str(excinfo.value)
    assert "through a2a" in message
    assert "exactly one message parameter" in message


def test_a_non_str_parameter_is_refused(tmp_path):
    """The two projected modalities are `Str` (a text `Part`) and `Bytes` (a
    file `Part`). An `Int` parameter is neither, so it has no A2A `Part` this
    slice projects and is refused rather than flattened."""
    services = """
service Agent {
  emission fn ask(count: Int) -> Str
}
"""
    write(tmp_path, services=services, base=WITHDRAW)
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    message = str(excinfo.value)
    assert "is `Int`, not a `Str` (a text `Part`) or a `Bytes`" in message


def test_a_non_str_return_is_refused(tmp_path):
    """Text out. A `Bool` return has no A2A text reply to be read from."""
    services = """
service Agent {
  emission fn ask(question: Str) -> Bool
}
"""
    write(tmp_path, services=services, base=WITHDRAW)
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    assert "return `Str` (the reply text)" in str(excinfo.value)


def test_on_failure_result_needs_result_str_str(tmp_path):
    """Under `on_failure(result)` the return must be `Result[Str, Str]`: text
    out in the `Ok`, the transport diagnostic in the `Err`."""
    base = WITHDRAW.replace("through a2a", "through a2a\n    on_failure(result)")
    # AGENT returns a bare `Str`, not a `Result`, so `on_failure(result)` on it
    # is refused.
    write(tmp_path, services=AGENT, base=base)
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    message = str(excinfo.value)
    # The shared `check_remotable` on_failure gate (D-424c.3) catches it before
    # the a2a-specific signature check even runs: `on_failure(result)` needs
    # every method to return `Result[T, E]`, and `ask` returns a bare `Str`.
    assert "on_failure(result)" in message
    assert "`Result[T, E]`" in message
    assert "`ask` returns" in message


# ----------------------------------------- the returned value is `Untrusted[T]`

# A consumer of the remote `Agent` that feeds the reply straight into an
# authority sink — a `Trusted[Str]` shell command. Nothing here knows or cares
# that `agent` is remote (D-424c.1); the taint qualifier on the synthesized
# crossing is what makes the difference visible to the checker.
SINK_CONSUMER = """
service Agent {
  emission fn ask(question: Str) -> Str
}
service Shell {
  emission fn go(q: Str) -> Str
}
extern emission[shell] fn run_cmd(cmd: Trusted[Str]) -> Str = @py { return "" }
component ShellSvc requires agent: Agent provides shell: Shell {
  provide shell {
    fn go(q) {
      let answer = emit agent.ask(q)
      let out = emit run_cmd(answer)
      return out
    }
  }
}
"""

TAINT_BASE = """
composition Net {
  use "services.rvl"
  row @shell from "services.rvl" provides shell
  remote @agent provides agent: Agent
    at host("agent.example:8443")
    through a2a
}
"""


def test_the_synthesized_crossing_returns_untrusted(tmp_path):
    """Item 424 D-424c.9, slice C3: every value a remote provider returns is
    `Untrusted[T]`. The synthesized `through a2a` extern declares it, so the
    checker propagates the taint to every consumer of the key."""
    text = _synth_source(tmp_path, WITHDRAW)
    assert "fn remote_agent_ask(question: Str) -> Untrusted[Str]" in text
    assert "EVERY RETURNED VALUE IS `Untrusted[T]`" in text


def test_a_remote_a2a_result_cannot_reach_an_authority_sink(tmp_path):
    """The C3 exit test on the A2A wire: a client result flowing into an
    outbound emission is refused (G9) without an `endorse`. A generated client
    looks exactly like a local provider at every call site, so the taint
    qualifier is what keeps a remote value from reaching a `Trusted[T]` sink
    invisibly."""
    write(tmp_path, services=SINK_CONSUMER, base=TAINT_BASE)
    with pytest.raises(RevlError) as excinfo:
        compile_composition(str(tmp_path / "base.rvl"), str(tmp_path))
    message = str(excinfo.value)
    assert "untrusted value (net)" in message
    assert "G9" in message


def test_an_endorse_on_the_flow_path_admits_the_remote_a2a_result(tmp_path):
    """...and admits with one. An `endorse[net]` granted on the operation and
    written on the data-flow path is the audited, policy-forbiddable downgrade
    that lets the vetted remote value reach the sink."""
    consumer = SINK_CONSUMER.replace(
        "emission fn go(q: Str) -> Str",
        "emission endorse[net] fn go(q: Str) -> Str").replace(
        "emit run_cmd(answer)",
        'emit run_cmd(endorse[net](answer, reason = "operator vetted"))')
    write(tmp_path, services=consumer, base=TAINT_BASE)
    document = compile_composition(str(tmp_path / "base.rvl"), str(tmp_path))
    assert document is not None


def test_the_result_wire_taints_the_whole_result(tmp_path):
    """`on_failure(result)` returns `Result[Str, Str]`; the reply object crossed
    the boundary, so the whole thing is `Untrusted[Result[Str, Str]]` (the
    fail-closed reading, and the shape a top-level `Untrusted[...]` source
    registers). The `Err` carries a locally-minted diagnostic, but marking it
    untrusted only over-fences it — never under-fences the peer's `Ok`."""
    base = TAINT_BASE.replace("through a2a", "through a2a\n    on_failure(result)")
    services = """
service Agent {
  emission fn ask(question: Str) -> Result[Str, Str]
}
service Shell {
  emission fn go(q: Str) -> Str
}
extern emission[shell] fn sink_r(r: Trusted[Result[Str, Str]]) -> Str
  = @py { return "" }
component ShellSvc requires agent: Agent provides shell: Shell {
  provide shell {
    fn go(q) {
      let r = emit agent.ask(q)
      let out = emit sink_r(r)
      return out
    }
  }
}
"""
    write(tmp_path, services=services, base=base)
    row = next(r for r in resolve(tmp_path).rows if r.label == "agent")
    text = resolve(tmp_path).sources[row.source]
    assert ("fn remote_agent_ask(question: Str) -> "
            "Untrusted[Result[Str, Str]]" in text)
    with pytest.raises(RevlError) as excinfo:
        compile_composition(str(tmp_path / "base.rvl"), str(tmp_path))
    assert "untrusted value (net)" in str(excinfo.value)


# ================================================================= sub-transports
# Item 439 remaining piece (4): the row bound only JSON-RPC 2.0; `through
# a2a_rest` binds the second JSON-body transport A2A 1.0.0 defines (HTTP+JSON/
# REST), the same one `revl import a2a` already reads off a card's
# `preferredTransport`. Both are a POST of a JSON body a `urllib` host carries;
# gRPC (binary HTTP/2 + protobuf) is not, and stays refused under any label.

REST = WITHDRAW.replace("through a2a", "through a2a_rest")


def test_through_a2a_rest_binds_and_resolves(tmp_path):
    """`through a2a_rest` resolves, carrying `a2a_rest` as its transport, and
    synthesizes a provider exactly as the JSON-RPC row does."""
    write(tmp_path, services=AGENT, base=REST)
    table = resolve(tmp_path)
    row, = table.rows
    assert row.remote.get("transport") == "a2a_rest"
    (_rel, text), = table.sources.items()
    assert "component RemoteAgentProvider provides agent: Agent" in text


def test_the_rest_wire_posts_to_the_message_send_path(tmp_path):
    """The one wire difference the importer already carries: REST POSTs the bare
    message (no JSON-RPC envelope) to `<endpoint>/v1/message:send`, and its reply
    IS the `Task`/`Message`, so there is no `result`/`error` envelope to unwrap."""
    text = _synth_source(tmp_path, REST)
    assert "HTTP+JSON/REST `POST /v1/message:send`" in text
    assert f"A2A {A2A_VERSION} over HTTP+JSON/REST" in text
    assert "/v1/message:send" in text
    # no JSON-RPC envelope on the REST wire
    assert '"jsonrpc": "2.0"' not in text
    assert '"method": "message/send"' not in text
    # still A2A: the text part and the skill metadata reference remain
    assert '"revl.skill": "ask"' in text


def test_the_rest_provider_compiles(tmp_path):
    write(tmp_path, services=AGENT, base=REST)
    assert compile_composition(str(tmp_path / "base.rvl"), str(tmp_path)) is not None


def test_grpc_is_still_refused_under_any_label(tmp_path):
    """gRPC is A2A 1.0.0's third transport and is a binary HTTP/2 + protobuf
    crossing, not the JSON POST this synthesizer emits, so it ships under no
    label — the honesty rule `check_transport` keeps."""
    base = WITHDRAW.replace("through a2a", "through grpc")
    write(tmp_path, services=AGENT, base=base)
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    message = str(excinfo.value)
    assert "grpc" in message
    assert "binds no transport by that name" in message


# ============================================================ non-text `Part`s
# Item 439 remaining piece (3): a `Str` is a text `Part`, and a `Bytes` is now a
# file `Part` with inline base64 bytes. A `DataPart` (structured JSON) still has
# no revl spelling here — it needs the canonical tagged encoding (slice C1) — so
# it is refused rather than flattened.

AGENT_FILE = """
service Agent {
  emission fn render(doc: Bytes) -> Bytes
}
"""


def test_a_bytes_method_synthesizes_a_file_part(tmp_path):
    """A `Bytes` parameter is sent as an A2A file `Part` with INLINE base64
    bytes (`FileWithBytes`); a `Bytes` return is read back the same way. The
    synthesized crossing still returns `Untrusted[Bytes]` (slice C3)."""
    text = _synth_source(tmp_path, WITHDRAW, services=AGENT_FILE)
    assert 'fn remote_agent_render(doc: Bytes) -> Untrusted[Bytes]' in text
    assert '"kind": "file", "file":' in text
    assert "b64encode" in text and "b64decode" in text
    assert '"kind": "text"' not in text


def test_a_bytes_file_method_compiles(tmp_path):
    write(tmp_path, services=AGENT_FILE, base=WITHDRAW)
    assert compile_composition(str(tmp_path / "base.rvl"), str(tmp_path)) is not None


def test_a_datapart_style_type_is_refused(tmp_path):
    """A record/ADT parameter is a `DataPart`'s structured JSON, which needs the
    canonical tagged encoding (slice C1) and is refused naming the method rather
    than flattened onto one text `Part`."""
    services = """
type Ask = { question: Str }
service Agent {
  emission fn ask(a: Ask) -> Str
}
"""
    write(tmp_path, services=services, base=WITHDRAW)
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    message = str(excinfo.value)
    assert "`Ask`" in message
    assert "a `Str` (a text `Part`) or a `Bytes`" in message


# ------------------------------------------------- no marked value crosses
# Item 439 question (2). `through a2a` binds `Str` and `Bytes` and NOTHING else,
# and neither of those is a marked type. So the F5 shape item 421 built (a marked
# value reaching a consumer through the failure text) has no crossing point at
# this boundary. This pins the precondition the note's question-(2) answer leans
# on: widen the modality subset and F5 reopens here.

@pytest.mark.parametrize(
    ("method", "needle"),
    [
        ("emission fn ask(question: Secret[Str]) -> Str", "Secret[Str]"),
        ("emission fn ask(question: Str) -> Secret[Str]", "Secret[Str]"),
    ],
    ids=["parameter", "return"],
)
def test_no_marked_value_can_cross_the_a2a_wire(tmp_path, method, needle):
    """A `Secret[Str]` has no `Part` in either direction, so the crossing is
    refused naming the method and the type rather than funnelled, flattened or
    rendered into the peer's fault text."""
    services = f"service Agent {{\n  {method}\n}}\n"
    write(tmp_path, services=services, base=WITHDRAW)
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    message = str(excinfo.value)
    assert f"`{needle}`" in message
    assert "`ask`" in message
    # The refusal is the MODALITY check, not the transport or the taint check:
    # the message names what a `Part` may carry.
    assert "a text `Part`" in message


# ---------------------------------------------- the generated crossing, executed

def _answer(sent: bytes, reply, correlate: bool = True):
    """What a COMPLIANT A2A 1.0.0 peer answers: the reply, carrying the request's
    own JSON-RPC `id` (item 439, the correlation gate).

    JSON-RPC 2.0 requires a response `id` to equal the request's, and the
    crossing refuses a reply that does not carry the identity it sent, so the
    stub replies below are written WITHOUT an id and this is where the
    protocol's own rule is applied. `correlate=False` is a peer answering with
    somebody else's id, which must be refused rather than read as a verdict.
    """
    if not isinstance(reply, dict) or "jsonrpc" not in reply:
        return reply
    if not correlate:
        return {**reply, "id": "somebody-elses-id"}
    return {**reply, "id": json.loads(sent).get("id")}


class _Ok:
    def __init__(self, v):
        self.v = v


class _Err:
    def __init__(self, e):
        self.e = e


def _run_row_body(reply, *, in_modality="text", out_modality="text",
                  in_band=False, rest=False, status=200, arg="ping",
                  correlate=True):
    """Execute a synthesized `through a2a[_rest]` `@py` body against a stubbed
    transport, the same technique `test_import_a2a._run_py_body` uses: the body
    is real code, so the file-part marshalling and the terminal-only refusal are
    tested directly rather than only greppa."""
    import io
    import textwrap
    import urllib.request

    body = _py_body_a2a("agent.example:8443", "render" if in_modality == "file"
                        else "ask", in_band, rest=rest, in_modality=in_modality,
                        out_modality=out_modality)
    src = "def _crossing(_arg):\n    _args = [_arg]\n" + textwrap.indent(
        textwrap.dedent(body), "    ")

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    calls = []

    class _Opener:
        def open(self, request, *a, **k):
            calls.append(request.data)
            if status >= 400:
                raise urllib.request.HTTPError(
                    request.full_url, status, "err", {}, io.BytesIO(b""))
            return _Resp(json.dumps(
                _answer(request.data, reply, correlate)).encode())

    ns = {"__name__": "generated", "Ok": _Ok, "Err": _Err}
    exec(compile(src, "<row-a2a-body>", "exec"), ns)
    original = urllib.request.build_opener
    urllib.request.build_opener = lambda *h: _Opener()
    try:
        return ns["_crossing"](arg), calls
    finally:
        urllib.request.build_opener = original


def test_jsonrpc_text_round_trip():
    reply = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "message", "parts": [{"kind": "text", "text": "pong"}]}}
    out, calls = _run_row_body(reply)
    assert out == "pong"
    sent = json.loads(calls[0])
    assert sent["method"] == "message/send"
    assert sent["params"]["message"]["parts"] == [{"kind": "text", "text": "ping"}]
    assert sent["params"]["message"]["metadata"]["revl.skill"] == "ask"


def test_rest_text_round_trip_sends_no_envelope():
    reply = {"kind": "message", "parts": [{"kind": "text", "text": "pong"}]}
    out, calls = _run_row_body(reply, rest=True)
    assert out == "pong"
    sent = json.loads(calls[0])
    # REST posts the bare message, no jsonrpc/method/id envelope
    assert "jsonrpc" not in sent and "method" not in sent
    assert sent["message"]["parts"] == [{"kind": "text", "text": "ping"}]


def test_file_part_round_trips_base64_bytes():
    """The load-bearing FilePart test: a `Bytes` argument is base64-encoded into
    a file `Part` on the wire, and a file `Part` reply is base64-decoded back to
    `Bytes` — end to end, executed."""
    import base64
    payload = b"\x00\x01PDF-ish\xff"
    b64 = base64.b64encode(b"reply-bytes").decode("ascii")
    reply = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "task", "id": "t", "status": {"state": "completed"},
        "artifacts": [{"parts": [{"kind": "file", "file": {"bytes": b64}}]}]}}
    out, calls = _run_row_body(reply, in_modality="file", out_modality="file",
                               arg=payload)
    assert out == b"reply-bytes"
    sent = json.loads(calls[0])
    part = sent["params"]["message"]["parts"][0]
    assert part["kind"] == "file"
    assert base64.b64decode(part["file"]["bytes"]) == payload


def test_a_uri_only_file_reply_is_a_fault():
    """A file `Part` that carried only a `uri` (a second crossing this slice does
    not make) is a fault, never a silently-empty answer."""
    reply = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "message", "parts": [{"kind": "file", "file": {"uri": "x"}}]}}
    with pytest.raises(RuntimeError) as excinfo:
        _run_row_body(reply, in_modality="file", out_modality="file", arg=b"x")
    assert "uri" in str(excinfo.value)


def test_a_non_terminal_task_faults_on_the_row_body():
    reply = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "task", "id": "t", "status": {"state": "working"}}}
    with pytest.raises(RuntimeError) as excinfo:
        _run_row_body(reply)
    assert "non-terminal" in str(excinfo.value)


def test_on_failure_result_returns_err_on_transport_failure():
    out, _ = _run_row_body({}, in_band=True, status=503)
    assert isinstance(out, _Err)
    assert "transport failure" in out.e


# ================================================ the four-op Task lifecycle (T1)
# Item 439 T1 (issue #118): `long_running` projects the four-op explicit-handle
# surface (`_start`/`_poll`/`_reply`/`_cancel`) over `message/send` / `tasks/get`
# / `tasks/cancel` instead of one terminal crossing, speaking the pure-revl
# vocabulary of `stdlib/a2a.rvl`. `docs/design/439-a2a-task-lifecycle.md`.

import io  # noqa: E402
import textwrap  # noqa: E402
import urllib.request  # noqa: E402

from revl.a2a_task import task_body  # noqa: E402

RESEARCHER = """
use "stdlib/a2a.rvl" { TaskRef, TaskState, TaskEvent }
service Researcher {
  emission fn research_start(message: Str) -> TaskRef
  emission fn research_poll(task: TaskRef) -> TaskEvent
  emission fn research_reply(task: TaskRef, message: Str) -> TaskEvent
  emission fn research_cancel(task: TaskRef) -> Unit
}
"""

LR = """
composition Net {
  use "services.rvl"
  remote @researcher provides researcher: Researcher
    at host("agent.example:8443")
    through a2a
    long_running
}
"""


def _write_lr(tmp_path: Path, base: str = LR, services: str = RESEARCHER) -> Path:
    (tmp_path / "stdlib").mkdir(exist_ok=True)
    (tmp_path / "stdlib" / "a2a.rvl").write_text(
        (ROOT / "stdlib" / "a2a.rvl").read_text(encoding="utf-8"),
        encoding="utf-8")
    write(tmp_path, services=services, base=base)
    return tmp_path


def _lr_source(tmp_path: Path, base: str = LR, services: str = RESEARCHER) -> str:
    _write_lr(tmp_path, base, services)
    table = resolve(tmp_path)
    return table.sources[next(r for r in table.rows if r.label == "researcher").source]


def test_long_running_resolves_and_projects_four_ops(tmp_path):
    """`long_running` carries onto the row table and synthesizes the four-op
    provider — the four crossings, not one terminal `message/send`."""
    _write_lr(tmp_path)
    table = resolve(tmp_path)
    row, = table.rows
    assert row.remote.get("longRunning") is True
    assert row.remote.get("transport") == "a2a"
    text = table.sources[row.source]
    for op in ("start", "poll", "reply", "cancel"):
        assert f"fn remote_researcher_research_{op}(" in text


def test_the_four_op_provider_compiles(tmp_path):
    """The synthesized four-op source is ordinary revl: `_link` runs G2/G3/G4
    over it, and the emitted `@py` bodies coexist with the `stdlib/a2a.rvl`
    vocabulary classes and the two pure gates in one module."""
    _write_lr(tmp_path)
    assert compile_composition(str(tmp_path / "base.rvl"), str(tmp_path)) is not None


def test_the_four_ops_speak_the_task_wire(tmp_path):
    """`_poll` -> `tasks/get`, `_cancel` -> `tasks/cancel`, `_reply` ->
    `message/send` + `taskId`, and every op rides `revl.skill`."""
    text = _lr_source(tmp_path)
    assert '"method": "tasks/get"' in text
    assert '"method": "tasks/cancel"' in text
    assert '"method": "message/send"' in text
    assert '"taskId": _task["id"]' in text
    assert '"revl.skill": "research"' in text
    assert '"id": _task["id"]' in text  # poll/cancel key the task by id


def test_every_four_op_return_is_untrusted(tmp_path):
    """Slice C3 on the lifecycle wire: the handle and every event are
    `Untrusted[T]` with origin `net`, so a `Done` payload cannot reach a
    `Trusted[T]` sink invisibly."""
    text = _lr_source(tmp_path)
    assert "fn remote_researcher_research_start(message: Str) -> Untrusted[TaskRef]" in text
    assert "fn remote_researcher_research_poll(task: TaskRef) -> Untrusted[TaskEvent]" in text
    assert "fn remote_researcher_research_cancel(task: TaskRef) -> Untrusted[Unit]" in text


def test_the_header_states_the_four_op_scope(tmp_path):
    """The header names the four-op lifecycle and that `_cancel` is the
    best-effort `tasks/cancel` compensation of `_start` (item 247), never an
    inverse."""
    text = _lr_source(tmp_path)
    assert "four-op A2A Task" in text or "four-op Task" in text
    assert "COMPENSATION of" in text
    assert "tasks/cancel" in text
    assert "A2A PEER IS A CLAIM" in text


# -- the two failure settlements / scope refusals -----------------------------

def test_long_running_on_the_default_wire_is_refused(tmp_path):
    base = LR.replace("    through a2a\n", "")
    _write_lr(tmp_path, base=base)
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    assert "long_running" in str(excinfo.value)
    assert "through a2a" in str(excinfo.value)


LR_REST = LR.replace("through a2a\n", "through a2a_rest\n")


def test_long_running_over_rest_binds_the_rest_task_paths(tmp_path):
    """A2A 1.0.0 specifies the four Task operations over BOTH of its JSON-body
    sub-transports, so `long_running` carries on `through a2a_rest` too — over
    the REST method paths, not a JSON-RPC envelope posted at a REST endpoint."""
    source = _lr_source(tmp_path, base=LR_REST)
    assert "/v1/message:send" in source
    assert '"/v1/tasks/"' in source
    assert '":cancel"' in source
    # and NOT the JSON-RPC envelope, which this peer does not speak
    assert '"method": "tasks/get"' not in source
    assert '"jsonrpc": "2.0"' not in source


def test_the_rest_four_op_provider_compiles(tmp_path):
    _write_lr(tmp_path, base=LR_REST)
    assert compile_composition(str(tmp_path / "base.rvl"), str(tmp_path)) is not None


def test_the_rest_four_op_header_names_the_rest_paths(tmp_path):
    """The header an operator reads states the sub-transport and the method
    paths it actually crosses, and says the correlation identity is one-way
    here — the check this wire cannot make is not described as if it could."""
    source = _lr_source(tmp_path, base=LR_REST)
    head = source[:source.index("component")]
    assert "over HTTP+JSON/REST, the four-op Task" in head
    assert "GET /v1/tasks/{id}" in head
    assert "POST /v1/tasks/{id}:cancel" in head
    assert "rides ONE WAY on this sub-transport" in head


def test_the_jsonrpc_four_ops_are_unchanged(tmp_path):
    """The non-vacuity control for the pair above: `through a2a` still speaks
    the JSON-RPC methods and knows nothing of the REST paths."""
    source = _lr_source(tmp_path)
    assert '"method": "tasks/get"' in source
    assert '"method": "tasks/cancel"' in source
    assert "/v1/tasks/" not in source


def test_a_service_not_in_the_four_op_shape_is_refused(tmp_path):
    services = """
use "stdlib/a2a.rvl" { TaskRef, TaskEvent }
service Researcher {
  emission fn research_start(message: Str) -> TaskRef
  emission fn research_poll(task: TaskRef) -> TaskEvent
}
"""
    _write_lr(tmp_path, services=services)
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    message = str(excinfo.value)
    assert "long_running" in message and "missing" in message


def test_a_wrongly_typed_task_op_is_refused(tmp_path):
    services = """
use "stdlib/a2a.rvl" { TaskRef, TaskEvent }
service Researcher {
  emission fn research_start(message: Str) -> Str
  emission fn research_poll(task: TaskRef) -> TaskEvent
  emission fn research_reply(task: TaskRef, message: Str) -> TaskEvent
  emission fn research_cancel(task: TaskRef) -> Unit
}
"""
    _write_lr(tmp_path, services=services)
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    assert "research_start" in str(excinfo.value) and "TaskRef" in str(excinfo.value)


# -- the returned event feed taints (C3 on the lifecycle) ---------------------

TASK_SINK = """
use "stdlib/a2a.rvl" { TaskRef, TaskState, TaskEvent }
service Researcher {
  emission fn research_start(message: Str) -> TaskRef
  emission fn research_poll(task: TaskRef) -> TaskEvent
  emission fn research_reply(task: TaskRef, message: Str) -> TaskEvent
  emission fn research_cancel(task: TaskRef) -> Unit
}
service Shell { emission fn go(t: TaskRef) -> TaskEvent }
extern emission[shell] fn sink(e: Trusted[TaskEvent]) -> Unit = @py { return None }
component ShellSvc requires researcher: Researcher provides shell: Shell {
  provide shell {
    fn go(t) {
      let ev = emit researcher.research_poll(t)
      emit sink(ev)
      return ev
    }
  }
}
"""


def test_a_task_event_cannot_reach_an_authority_sink(tmp_path):
    """A polled `TaskEvent` (a `Done` payload among them) flowing into a
    `Trusted[T]` sink is refused (G9) without an `endorse`."""
    _write_lr(tmp_path, services=TASK_SINK)
    with pytest.raises(RevlError) as excinfo:
        compile_composition(str(tmp_path / "base.rvl"), str(tmp_path))
    message = str(excinfo.value)
    assert "untrusted value (net)" in message and "G9" in message


def test_an_endorse_admits_the_task_event(tmp_path):
    consumer = TASK_SINK.replace(
        "emission fn go(t: TaskRef) -> TaskEvent",
        "emission endorse[net] fn go(t: TaskRef) -> TaskEvent").replace(
        "emit sink(ev)",
        'emit sink(endorse[net](ev, reason = "operator vetted"))')
    _write_lr(tmp_path, services=consumer)
    assert compile_composition(str(tmp_path / "base.rvl"), str(tmp_path)) is not None


# -- the generated bodies, executed against a fake A2A server -----------------

class _TaskOk:
    def __init__(self, v):
        self.v = v


def _run_task_body(kind, reply, *args, status=200, label="researcher",
                   correlate=True):
    """Execute a synthesized four-op `@py` body against a stubbed transport, the
    technique `_run_row_body` uses — the vocabulary constructors and the pure
    `task_state_from_wire` gate are injected the way the emitted module carries
    them."""
    body = task_body(kind, "https://agent.example:8443", "research", label=label)
    names = {"start": ["_a"], "poll": ["_a"], "reply": ["_a", "_b"],
             "cancel": ["_a"]}[kind]
    sig = ", ".join(names)
    src = (f"def _crossing({sig}):\n    _args = [{sig}]\n"
           + textwrap.indent(textwrap.dedent(body), "    "))

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    calls = []

    class _Opener:
        def open(self, request, *a, **k):
            calls.append(json.loads(request.data))
            if status >= 400:
                raise urllib.request.HTTPError(
                    request.full_url, status, "err", {}, io.BytesIO(b""))
            return _Resp(json.dumps(
                _answer(request.data, reply, correlate)).encode())

    class _Ev:
        def __init__(self, tag, value=None):
            self.tag = tag
            self.value = value

        def __eq__(self, other):
            return (isinstance(other, _Ev) and other.tag == self.tag
                    and other.value == self.value)

    ns = {
        "__name__": "generated",
        "Status": lambda v: _Ev("Status", v),
        "Done": lambda v: _Ev("Done", v),
        "Message": lambda v: _Ev("Message", v),
        "task_state_from_wire": lambda s: s,
    }
    exec(compile(src, "<task-body>", "exec"), ns)
    original = urllib.request.build_opener
    urllib.request.build_opener = lambda *h: _Opener()
    try:
        return ns["_crossing"](*args), calls, _Ev
    finally:
        urllib.request.build_opener = original


def test_start_returns_a_task_ref():
    reply = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "task", "id": "t-42", "contextId": "c-1",
        "status": {"state": "working"}}}
    out, calls, _Ev = _run_task_body("start", reply, "survey the field")
    assert out == {"id": "t-42", "context": "c-1"}
    assert calls[0]["method"] == "message/send"
    assert calls[0]["params"]["message"]["metadata"]["revl.skill"] == "research"


def test_poll_walks_working_to_done():
    working = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "task", "status": {"state": "working"}}}
    out, calls, _Ev = _run_task_body("poll", working, {"id": "t-42", "context": None})
    assert out == _Ev("Status", "working")
    assert calls[0]["method"] == "tasks/get" and calls[0]["params"]["id"] == "t-42"
    done = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "task", "status": {"state": "completed"},
        "artifacts": [{"parts": [{"kind": "text", "text": "the answer"}]}]}}
    out, _c, _Ev = _run_task_body("poll", done, {"id": "t-42", "context": None})
    assert out == _Ev("Done", "the answer")


def test_input_required_then_reply_advances():
    prompt = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "task", "status": {"state": "input-required"}}}
    out, _c, _Ev = _run_task_body("poll", prompt, {"id": "t", "context": None})
    assert out == _Ev("Status", "input-required")
    advanced = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "task", "status": {"state": "working"}}}
    out, calls, _Ev = _run_task_body(
        "reply", advanced, {"id": "t-42", "context": None}, "my input")
    assert out == _Ev("Status", "working")
    assert calls[0]["method"] == "message/send"
    assert calls[0]["params"]["message"]["taskId"] == "t-42"


def test_cancel_posts_tasks_cancel_and_returns_unit():
    reply = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "task", "status": {"state": "canceled"}}}
    out, calls, _Ev = _run_task_body("cancel", reply, {"id": "t-42", "context": None})
    assert out is None
    assert calls[0]["method"] == "tasks/cancel" and calls[0]["params"]["id"] == "t-42"


# -- the same four ops over A2A 1.0.0 HTTP+JSON/REST --------------------------
# A2A 1.0.0 specifies the Task operations over both of its JSON-body
# sub-transports. The REST wire differs in exactly two observable ways and this
# block pins both: the method is the PATH (and the reply is the bare
# `Task`/`Message`, so there is no envelope to gate), and the task id — which is
# PEER-AUTHORED, it came back from `_start` — becomes URL path structure.

REST_ROOT = "https://agent.example:8443/a2a"


def _run_rest_task_body(kind, reply, *args, status=200, label="researcher"):
    """Execute a synthesized four-op REST `@py` body against a stubbed
    transport. The reply IS the `Task`/`Message` on this wire, so nothing is
    wrapped; each call is recorded as `(url, method, body)` because the method
    and the URL are what the REST binding chooses and a GET carries no body."""
    body = task_body(kind, REST_ROOT, "research", label=label, rest=True)
    names = {"start": ["_a"], "poll": ["_a"], "reply": ["_a", "_b"],
             "cancel": ["_a"]}[kind]
    sig = ", ".join(names)
    src = (f"def _crossing({sig}):\n    _args = [{sig}]\n"
           + textwrap.indent(textwrap.dedent(body), "    "))

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    calls = []

    class _Opener:
        def open(self, request, *a, **k):
            calls.append((request.full_url, request.get_method(), request.data))
            if status >= 400:
                raise urllib.request.HTTPError(
                    request.full_url, status, "err", {}, io.BytesIO(b""))
            return _Resp(json.dumps(reply).encode())

    class _Ev:
        def __init__(self, tag, value=None):
            self.tag = tag
            self.value = value

        def __eq__(self, other):
            return (isinstance(other, _Ev) and other.tag == self.tag
                    and other.value == self.value)

    ns = {
        "__name__": "generated",
        "Status": lambda v: _Ev("Status", v),
        "Done": lambda v: _Ev("Done", v),
        "Message": lambda v: _Ev("Message", v),
        "task_state_from_wire": lambda s: s,
    }
    exec(compile(src, "<rest-task-body>", "exec"), ns)
    original = urllib.request.build_opener
    urllib.request.build_opener = lambda *h: _Opener()
    try:
        return ns["_crossing"](*args), calls, _Ev
    finally:
        urllib.request.build_opener = original


def test_rest_start_posts_the_bare_message_and_returns_a_task_ref():
    reply = {"kind": "task", "id": "t-42", "contextId": "c-1",
             "status": {"state": "working"}}
    out, calls, _Ev = _run_rest_task_body("start", reply, "survey the field")
    assert out == {"id": "t-42", "context": "c-1"}
    url, method, sent = calls[0]
    assert url == REST_ROOT + "/v1/message:send" and method == "POST"
    sent = json.loads(sent)
    # no envelope: the REST wire posts the bare `{ message }`
    assert "jsonrpc" not in sent and "method" not in sent and "id" not in sent
    assert sent["message"]["parts"] == [{"kind": "text",
                                         "text": "survey the field"}]
    assert sent["message"]["metadata"]["revl.skill"] == "research"
    # the correlation identity still rides, ONE WAY, for the peer's log and ours
    assert sent["message"]["metadata"]["revl.correlation"]


def test_rest_poll_gets_the_task_path_and_walks_working_to_done():
    working = {"kind": "task", "status": {"state": "working"}}
    out, calls, _Ev = _run_rest_task_body(
        "poll", working, {"id": "t-42", "context": None})
    assert out == _Ev("Status", "working")
    url, method, sent = calls[0]
    assert url == REST_ROOT + "/v1/tasks/t-42"
    assert method == "GET" and sent is None
    done = {"kind": "task", "status": {"state": "completed"},
            "artifacts": [{"parts": [{"kind": "text", "text": "the answer"}]}]}
    out, _c, _Ev = _run_rest_task_body("poll", done,
                                       {"id": "t-42", "context": None})
    assert out == _Ev("Done", "the answer")


def test_rest_reply_posts_message_send_with_the_task_id():
    advanced = {"kind": "task", "status": {"state": "working"}}
    out, calls, _Ev = _run_rest_task_body(
        "reply", advanced, {"id": "t-42", "context": None}, "my input")
    assert out == _Ev("Status", "working")
    url, method, sent = calls[0]
    assert url == REST_ROOT + "/v1/message:send" and method == "POST"
    assert json.loads(sent)["message"]["taskId"] == "t-42"


def test_rest_cancel_posts_the_cancel_verb_and_returns_unit():
    reply = {"kind": "task", "status": {"state": "canceled"}}
    out, calls, _Ev = _run_rest_task_body(
        "cancel", reply, {"id": "t-42", "context": None})
    assert out is None
    url, method, _sent = calls[0]
    assert url == REST_ROOT + "/v1/tasks/t-42:cancel" and method == "POST"


def test_a_peer_authored_task_id_cannot_steer_the_rest_crossing():
    """The load-bearing REST refusal. The task id came back from the PEER's own
    `_start` reply, and on this sub-transport it becomes URL PATH STRUCTURE. A
    peer that answers `_start` with `../v1/message:send` would otherwise choose
    which request this composition sends next — same declared endpoint, a
    different method. The id is percent-encoded WHOLE, so it stays exactly one
    path segment and the crossing stays on the task path."""
    hostile = "../v1/message:send"
    working = {"kind": "task", "status": {"state": "working"}}
    _out, calls, _Ev = _run_rest_task_body(
        "poll", working, {"id": hostile, "context": None})
    url, method, _sent = calls[0]
    assert url == REST_ROOT + "/v1/tasks/..%2Fv1%2Fmessage%3Asend"
    assert method == "GET"
    # the peer did not reach the send path, and did not climb out of the segment
    assert not url.endswith("/v1/message:send")
    assert "/../" not in url
    # the same on the cancel verb, which appends AFTER the encoded segment
    _out, calls, _Ev = _run_rest_task_body(
        "cancel", {"kind": "task", "status": {"state": "canceled"}},
        {"id": hostile, "context": None})
    assert calls[0][0] == REST_ROOT + "/v1/tasks/..%2Fv1%2Fmessage%3Asend:cancel"


@pytest.mark.parametrize("handle", [
    {"id": "", "context": None},
    {"id": None, "context": None},
    {"context": None},
])
def test_a_rest_task_handle_with_no_usable_id_faults(handle):
    """The shape gate in front of the encoder: a handle carrying no usable id
    refuses as this crossing's own declared fault, not as a `TypeError` no
    settlement classifies."""
    with pytest.raises(RuntimeError) as excinfo:
        _run_rest_task_body("poll", {"kind": "task"}, handle)
    fault = excinfo.value
    assert "no task id" in str(fault)
    assert getattr(fault, "_revl_transport_fault", False) is True


def test_a_rest_task_reply_about_another_task_is_refused():
    """The envelope gate does not exist on this wire; the TASK identity gate
    still does, and it is what stops a REST peer answering about another task
    from being read as a lifecycle event for ours."""
    other = {"kind": "task", "id": "t-99", "status": {"state": "completed"}}
    with pytest.raises(RuntimeError) as excinfo:
        _run_rest_task_body("poll", other, {"id": "t-42", "context": None})
    assert "different task" in str(excinfo.value)


@pytest.mark.parametrize("reply,needle", [
    ("[1, 2]", "no result object"),
    ('"ok"', "no result object"),
    ("null", "no result object"),
])
def test_an_unshaped_rest_reply_refuses_rather_than_being_read(reply, needle):
    with pytest.raises(RuntimeError) as excinfo:
        _run_rest_task_body("poll", json.loads(reply),
                            {"id": "t", "context": None})
    assert needle in str(excinfo.value)


def test_the_rest_four_op_wire_funnels_the_peers_task_state():
    """F5 at this boundary holds on the REST four-op wire too: a peer that
    reflects the caller's own argument back as a task `state` cannot put those
    bytes on our error channel. `_reply` is the op that carries an argument."""
    echo = {"kind": "nonsense-" + "correlation-bait"}
    with pytest.raises(RuntimeError) as excinfo:
        _run_rest_task_body("reply", echo, {"id": "t", "context": None},
                            "correlation-bait")
    message = str(excinfo.value)
    assert "correlation-bait" not in message
    assert "<redacted:arg>" in message
    assert "unexpected result kind" in message


@pytest.mark.parametrize("kind,args", [
    ("start", ("x",)),
    ("poll", ({"id": "t", "context": None},)),
    ("reply", ({"id": "t", "context": None}, "m")),
    ("cancel", ({"id": "t", "context": None},)),
])
def test_a_dead_peer_faults_on_every_rest_crossing(kind, args):
    """T0 holds identically on the REST sub-transport: every one of the four
    crossings raises the MARKED `TransportFault` the activation runtime maps to
    provider withdrawal, carrying the row label and the crossing."""
    with pytest.raises(RuntimeError) as excinfo:
        _run_rest_task_body(kind, {}, *args, status=503)
    fault = excinfo.value
    assert getattr(fault, "_revl_transport_fault", False) is True
    assert fault._revl_row == "researcher"
    assert fault._revl_crossing == f"research_{kind}"


@pytest.mark.parametrize("kind,args", [
    ("start", ("x",)),
    ("poll", ({"id": "t", "context": None},)),
    ("reply", ({"id": "t", "context": None}, "m")),
    ("cancel", ({"id": "t", "context": None},)),
])
def test_a_dead_peer_faults_and_names_the_crossing(kind, args):
    """A peer that stops answering raises a marked `TransportFault` carrying the
    row label and the crossing — the T0 handle the activation runtime maps to
    provider WITHDRAWAL, on every one of the four crossings."""
    with pytest.raises(RuntimeError) as excinfo:
        _run_task_body(kind, {}, *args, status=503)
    fault = excinfo.value
    assert getattr(fault, "_revl_transport_fault", False) is True
    assert fault._revl_row == "researcher"
    assert fault._revl_crossing == f"research_{kind}"


# ==================================== the boundary gates (item 439, slice B1)
# `docs/design/439-a2a-transport-binding.md` question (2)'s fourth layer, and
# the correlation identity every crossing carries. Two rules, and the second is
# the first one's consequence: a reply that does not carry back the identity the
# crossing sent is not a verdict, and peer-authored text that IS read is
# funnelled (item 421 F5) before it reaches the consumer. `revl.a2a_boundary`.

def test_every_crossing_carries_one_correlation_identity():
    """The envelope `id` and the `revl.correlation` metadata member are the SAME
    value, so the identity the peer logs is the identity the reply is checked
    against, and it is fresh per crossing."""
    reply = {"jsonrpc": "2.0", "result": {
        "kind": "message", "parts": [{"kind": "text", "text": "pong"}]}}
    _out, calls = _run_row_body(reply)
    sent = json.loads(calls[0])
    corr = sent["id"]
    assert corr and sent["params"]["message"]["metadata"]["revl.correlation"] == corr
    _out2, calls2 = _run_row_body(reply)
    assert json.loads(calls2[0])["id"] != corr


def test_a_reply_that_does_not_correlate_is_refused_not_read():
    """The load-bearing refusal: a peer (or anything between us and it) that
    answers with somebody else's id is a crossing that did not answer, never a
    value. JSON-RPC 2.0 requires the response id to equal the request's, so this
    refuses only a peer already off protocol."""
    reply = {"jsonrpc": "2.0", "result": {
        "kind": "message", "parts": [{"kind": "text", "text": "pong"}]}}
    with pytest.raises(RuntimeError) as excinfo:
        _run_row_body(reply, correlate=False)
    assert "correlation id" in str(excinfo.value)
    # and the peer's own id is NOT echoed into our fault text
    assert "somebody-elses-id" not in str(excinfo.value)


def test_an_uncorrelated_reply_is_an_err_under_on_failure_result():
    """Same refusal, the other settlement: in band it is an `Err`, and it is
    still never the peer's value."""
    reply = {"jsonrpc": "2.0", "result": {
        "kind": "message", "parts": [{"kind": "text", "text": "pong"}]}}
    out, _calls = _run_row_body(reply, in_band=True, correlate=False)
    assert isinstance(out, _Err) and "correlation id" in out.e


@pytest.mark.parametrize("reply,needle", [
    ([1, 2, 3], "was not a JSON-RPC object"),
    ("just a string", "was not a JSON-RPC object"),
    ({"id": "x", "result": {"kind": "message", "parts": []}},
     "did not claim JSON-RPC 2.0"),
    ({"jsonrpc": "2.0", "result": "ok"}, "no result object"),
])
def test_an_unparseable_reply_refuses_rather_than_being_read(reply, needle):
    """Fail-closed on SHAPE too: every member is read only after the reply is
    known to be the object it claims to be. A peer that answers a JSON array, a
    string, or a truthy non-object `result` is a crossing that failed, not an
    `AttributeError` neither settlement classifies."""
    with pytest.raises(RuntimeError) as excinfo:
        _run_row_body(reply)
    assert needle in str(excinfo.value)
    assert not isinstance(excinfo.value, AttributeError)


def test_a_peer_cannot_echo_the_callers_argument_onto_our_error_channel():
    """Question (2)'s fourth layer, the exit test: a peer-supplied `error.code`
    that echoes the caller's own argument text is scrubbed the way the placement
    seam's failure channel scrubs it (`backends/python/confidential.py`'s
    `redact_call_text`, the funnel `backends/python/bridge.py` joins), and the
    SHAPE of the failure survives so it is still worth reading."""
    secret = "INV-4242-not-for-the-console"
    reply = {"jsonrpc": "2.0", "error": {"code": f"rejected {secret}"}}
    with pytest.raises(RuntimeError) as excinfo:
        _run_row_body(reply, arg=secret)
    message = str(excinfo.value)
    assert secret not in message
    assert "<redacted:arg>" in message
    assert message.startswith("a2a: JSON-RPC error rejected ")


def test_the_funnel_matches_exactly_and_never_by_pattern():
    """The negative exit test. The funnel is an EXACT match against this call's
    own arguments, so a peer string that merely resembles one is left verbatim:
    a pattern would shred diagnostics and would claim a confidentiality bound
    nothing here can hold."""
    reply = {"jsonrpc": "2.0", "error": {"code": "rejected INV-4242"}}
    with pytest.raises(RuntimeError) as excinfo:
        _run_row_body(reply, arg="INV-4242-not-for-the-console")
    assert "rejected INV-4242" in str(excinfo.value)
    assert "<redacted:arg>" not in str(excinfo.value)


@pytest.mark.parametrize("state", ["working", "failed"])
def test_the_task_state_is_funnelled_too(state):
    """A peer chooses its own `state` string, and the boundary renders it. Both
    the non-terminal refusal and the terminal-but-not-done one go through the
    funnel."""
    secret = "INV-9001-not-for-the-console"
    reply = {"jsonrpc": "2.0", "result": {
        "kind": "task", "id": "t", "status": {"state": f"{state} {secret}"}}}
    with pytest.raises(RuntimeError) as excinfo:
        _run_row_body(reply, arg=secret)
    assert secret not in str(excinfo.value)
    assert "<redacted:arg>" in str(excinfo.value)


def test_the_in_band_error_text_is_funnelled(   ):
    """`on_failure(result)` hands the failure to the consumer IN BAND, which is
    the same disclosure with a different shape, so it is funnelled identically."""
    secret = "INV-4242-not-for-the-console"
    reply = {"jsonrpc": "2.0", "error": {"code": f"rejected {secret}"}}
    out, _calls = _run_row_body(reply, in_band=True, arg=secret)
    assert isinstance(out, _Err)
    assert secret not in out.e and "<redacted:arg>" in out.e


def test_a_bytes_argument_is_funnelled_in_both_of_its_faces():
    """A `Bytes` argument wears more renderings than a `Str`, and an encoder
    renders one of them (`confidential._needles`), so both faces are needles."""
    payload = b"INV-4242-not-for-the-console"
    reply = {"jsonrpc": "2.0", "error": {
        "code": "rejected " + payload.decode()}}
    with pytest.raises(RuntimeError) as excinfo:
        _run_row_body(reply, in_modality="file", out_modality="file",
                      arg=payload)
    assert payload.decode() not in str(excinfo.value)
    assert "<redacted:arg>" in str(excinfo.value)


def test_the_rest_wire_carries_the_identity_one_way_and_still_gates_the_shape():
    """A2A 1.0.0's REST reply is the bare `Task`/`Message` and echoes no
    envelope, so the identity rides one-way (for the peer's log and ours) and
    the shape gate is what stays. Nothing is claimed that the wire cannot
    check."""
    reply = {"kind": "message", "parts": [{"kind": "text", "text": "pong"}]}
    out, calls = _run_row_body(reply, rest=True)
    assert out == "pong"
    metadata = json.loads(calls[0])["message"]["metadata"]
    assert metadata["revl.correlation"]
    with pytest.raises(RuntimeError) as excinfo:
        _run_row_body([1, 2], rest=True)
    assert "no result object" in str(excinfo.value)


# ---------------------------------------- the same gates on the four-op wire

def test_a_task_reply_about_another_task_is_refused():
    """The Task-level correlation: the envelope gate says the reply answers THIS
    crossing, and this one says it describes THIS task. A `tasks/get` answered
    with another task's status would otherwise be read as a lifecycle event for
    ours."""
    other = {"jsonrpc": "2.0", "result": {
        "kind": "task", "id": "t-99", "status": {"state": "completed"}}}
    with pytest.raises(RuntimeError) as excinfo:
        _run_task_body("poll", other, {"id": "t-42", "context": None})
    assert "different task" in str(excinfo.value)


def test_a_cancel_acknowledged_for_another_task_is_refused():
    """`tasks/cancel` is best-effort (item 247) and a peer that answers is
    honoured whatever state it reports, but a peer that answers about ANOTHER
    task has not acknowledged ours."""
    other = {"jsonrpc": "2.0", "result": {
        "kind": "task", "id": "t-99", "status": {"state": "canceled"}}}
    with pytest.raises(RuntimeError) as excinfo:
        _run_task_body("cancel", other, {"id": "t-42", "context": None})
    assert "different task" in str(excinfo.value)
    # ...while an acknowledgement that names no task at all still lands
    bare = {"jsonrpc": "2.0", "result": {"kind": "task",
                                         "status": {"state": "canceled"}}}
    out, _calls, _Ev = _run_task_body("cancel", bare,
                                      {"id": "t-42", "context": None})
    assert out is None


def test_an_uncorrelated_task_reply_is_refused_on_every_op():
    """All four crossings carry the identity and all four refuse a reply that
    does not carry it back."""
    reply = {"jsonrpc": "2.0", "result": {
        "kind": "task", "id": "t-42", "status": {"state": "working"}}}
    for kind, args in (("start", ("x",)),
                       ("poll", ({"id": "t-42", "context": None},)),
                       ("reply", ({"id": "t-42", "context": None}, "m")),
                       ("cancel", ({"id": "t-42", "context": None},))):
        with pytest.raises(RuntimeError) as excinfo:
            _run_task_body(kind, reply, *args, correlate=False)
        assert "correlation id" in str(excinfo.value), kind


def test_the_four_op_wire_funnels_the_peers_error_code():
    """The funnel is on the four-op wire too, closing over whatever that op was
    called with (a `TaskRef` as well as a message)."""
    secret = "survey-INV-4242-not-for-the-console"
    reply = {"jsonrpc": "2.0", "error": {"code": f"rejected {secret}"}}
    with pytest.raises(RuntimeError) as excinfo:
        _run_task_body("reply", reply, {"id": "t-42", "context": None}, secret)
    assert secret not in str(excinfo.value)
    assert "<redacted:arg>" in str(excinfo.value)


# ------------------------------------------------- G8: the boundary is visible

def test_the_a2a_crossing_is_on_the_g8_audit_surface(tmp_path):
    """`docs/design/439-a2a-task-lifecycle.md` decision 3 states G8 as "the
    boundary surface is the four (or one) synthesized externs, enumerable by
    `revl audit`". A remote row synthesizes an ORDINARY provider holding
    ORDINARY externs, which is what makes that true; this pins it, so a future
    change to the synthesized shape cannot take the boundary off the surface an
    operator reads."""
    from revl.audit_diff import audit_report  # noqa: PLC0415

    # the taint fixture's sink is `Trusted[Str]`, which is refused on purpose
    # (G9); the audit surface is about a composition that ADMITS, so this one
    # takes the remote answer into an untrusted sink.
    write(tmp_path, services=SINK_CONSUMER.replace("Trusted[Str]", "Str"),
          base=TAINT_BASE)
    report = audit_report(compile_composition(str(tmp_path / "base.rvl"),
                                             str(tmp_path)))
    provider = report["boundary"]["RemoteAgentProvider"]
    crossing, = provider["externs"]
    assert crossing["name"] == "remote_agent_ask"
    assert crossing["class"] == "emission"
    assert crossing["capabilities"] == ["net.agent_example"]


def test_all_four_task_crossings_are_on_the_g8_audit_surface(tmp_path):
    """The `long_running` form projects four crossings, and all four are on the
    surface: an operator counting what leaves the process sees four, not one."""
    from revl.audit_diff import audit_report  # noqa: PLC0415

    _write_lr(tmp_path)
    report = audit_report(compile_composition(str(tmp_path / "base.rvl"),
                                              str(tmp_path)))
    provider = report["boundary"]["RemoteResearcherProvider"]
    assert sorted(e["name"] for e in provider["externs"]) == [
        "remote_researcher_research_cancel",
        "remote_researcher_research_poll",
        "remote_researcher_research_reply",
        "remote_researcher_research_start",
    ]
    assert {e["class"] for e in provider["externs"]} == {"emission"}
    assert {tuple(e["capabilities"]) for e in provider["externs"]} == {
        ("net.agent_example",)}


# ====================== the reply's SHAPE, on every peer-authored member (T1b)
# Item 439 (issue #118). `#1030`'s rule one layer in: a peer-authored VALUE was
# already shape-checked where it becomes URL path structure, and this block
# holds the same line for the peer-authored CONTAINERS a reply is read through.
#
# A2A 1.0.0 says a Task's `status` is a `TaskStatus` object, its `artifacts` a
# list of `Artifact` objects, a `Message`'s `parts` a list of `Part` objects and
# a Task's `contextId` an optional string. A peer is a CLAIM (item 329), so
# none of that is a fact about the bytes that arrive. Read member by member, a
# `status` that is a string or an `artifacts` that is a list of strings raises
# `AttributeError` / `TypeError` straight out of the generated body.
#
# THE FAILURE DIRECTION, which is why this is a gate and not tidiness: an
# exception no settlement classifies is not the settlement the row DECLARED.
# Provider withdrawal keys on the `_revl_transport_fault` marker (slice T0), so
# an unmarked host error leaves wired the provider `on_failure(withdraw)` says
# to withdraw, and is not the `Err` `on_failure(result)` says to return. A peer
# could therefore choose whether the declared settlement ran at all, by
# answering valid JSON of the wrong shape. After the gates every one of these
# is this crossing's own declared fault, so the settlement the row declares is
# the settlement that runs.

def _task(**members):
    """A JSON-RPC reply whose `result` is a Task for `t-42` (so the identity
    gate, which runs first, passes and the SHAPE is what is under test)."""
    return {"jsonrpc": "2.0", "id": "1",
            "result": {"kind": "task", "id": "t-42", **members}}


MALFORMED = [
    # `status` is not an object
    (_task(status="working"), "task status was not an object"),
    (_task(status=["working"]), "task status was not an object"),
    (_task(status=7), "task status was not an object"),
    # `status.state` is not a string
    (_task(status={"state": {"evil": 1}}), "task status state was not a string"),
    (_task(status={"state": ["working"]}), "task status state was not a string"),
    # `artifacts` is not a list of objects
    (_task(status={"state": "completed"}, artifacts={"a": 1}),
     "task artifacts were not a list"),
    (_task(status={"state": "completed"}, artifacts="boom"),
     "task artifacts were not a list"),
    (_task(status={"state": "completed"}, artifacts=["boom"]),
     "task artifact was not an object"),
    # an artifact's `parts` is not a list of objects
    (_task(status={"state": "completed"}, artifacts=[{"parts": "boom"}]),
     "task artifact parts were not a list"),
    (_task(status={"state": "completed"}, artifacts=[{"parts": ["boom"]}]),
     "reply part was not an object"),
]

MALFORMED_MESSAGE = [
    ({"jsonrpc": "2.0", "id": "1", "result": {"kind": "message", "parts": "boom"}},
     "reply parts were not a list"),
    ({"jsonrpc": "2.0", "id": "1", "result": {"kind": "message", "parts": {"a": 1}}},
     "reply parts were not a list"),
    ({"jsonrpc": "2.0", "id": "1", "result": {"kind": "message", "parts": ["boom"]}},
     "reply part was not an object"),
]


@pytest.mark.parametrize("reply,needle", MALFORMED + MALFORMED_MESSAGE)
def test_a_wrongly_shaped_task_reply_is_the_crossings_own_fault(reply, needle):
    """`_poll` over JSON-RPC: every peer-authored container is checked, and a
    wrong one settles as the MARKED `TransportFault` the activation runtime maps
    to withdrawal, never as a host exception that escapes the settlement."""
    with pytest.raises(RuntimeError) as excinfo:
        _run_task_body("poll", reply, {"id": "t-42", "context": None})
    fault = excinfo.value
    assert needle in str(fault)
    assert getattr(fault, "_revl_transport_fault", False) is True
    assert fault._revl_row == "researcher"
    assert fault._revl_crossing == "research_poll"


@pytest.mark.parametrize("reply,needle", MALFORMED + MALFORMED_MESSAGE)
def test_a_wrongly_shaped_reply_faults_on_the_reply_crossing_too(reply, needle):
    """`_reply` reads a Task the same way `_poll` does, so the same gates guard
    it: a gate on one of the two event-returning ops and not the other would be
    a peer read differently depending on which one asked."""
    with pytest.raises(RuntimeError) as excinfo:
        _run_task_body("reply", reply, {"id": "t-42", "context": None}, "answer")
    assert needle in str(excinfo.value)
    assert getattr(excinfo.value, "_revl_transport_fault", False) is True


@pytest.mark.parametrize("context", [{"evil": 1}, ["c-1"], 7, True])
def test_a_non_string_context_id_is_refused_at_start(context):
    """The second peer-authored identifier this binding KEEPS. `stdlib/a2a.rvl`
    declares `TaskRef.context: Opt[Str]`, so an unchecked one puts arbitrary
    peer-authored JSON structure behind a type that says string, everywhere a
    `TaskRef` goes — the task id's own failure direction (#1030), one field
    over. It is a fault and not a coercion: dropping it to `None` would discard
    a grouping identity the peer asserted and `str()` would fabricate one."""
    reply = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "task", "id": "t-42", "contextId": context,
        "status": {"state": "working"}}}
    with pytest.raises(RuntimeError) as excinfo:
        _run_task_body("start", reply, "survey the field")
    fault = excinfo.value
    assert "contextId was not a string" in str(fault)
    assert getattr(fault, "_revl_transport_fault", False) is True
    assert fault._revl_crossing == "research_start"


@pytest.mark.parametrize("reply,needle", [
    ({"kind": "task", "id": "t-42", "status": "working"},
     "task status was not an object"),
    ({"kind": "task", "id": "t-42", "status": {"state": "completed"},
      "artifacts": ["boom"]}, "task artifact was not an object"),
    ({"kind": "message", "parts": ["boom"]}, "reply part was not an object"),
])
def test_the_rest_wire_shape_gates_its_reply_the_same_way(reply, needle):
    """The REST sub-transport has no envelope to gate, which is exactly why the
    shape gates matter more there and not less: they are the checks this wire
    CAN make."""
    with pytest.raises(RuntimeError) as excinfo:
        _run_rest_task_body("poll", reply, {"id": "t-42", "context": None})
    assert needle in str(excinfo.value)
    assert getattr(excinfo.value, "_revl_transport_fault", False) is True


def test_a_rest_start_refuses_a_non_string_context_id():
    reply = {"kind": "task", "id": "t-42", "contextId": {"evil": 1},
             "status": {"state": "working"}}
    with pytest.raises(RuntimeError) as excinfo:
        _run_rest_task_body("start", reply, "survey the field")
    assert "contextId was not a string" in str(excinfo.value)


# -- the same gates on the TERMINAL single-crossing wire ----------------------
# One boundary, three emitters: the terminal `through a2a` row reads a peer's
# reply through the same containers, so it gets the same gates. Both
# settlements are pinned, because they are the two the failure direction is
# about: `withdraw` must raise the marked fault and `result` must return the
# declared `Err`, and before the gates neither happened.

@pytest.mark.parametrize("result,needle", [
    ({"kind": "task", "status": "completed"}, "task status was not an object"),
    ({"kind": "task", "status": {"state": {"x": 1}}},
     "task status state was not a string"),
    ({"kind": "task", "status": {"state": "completed"}, "artifacts": "boom"},
     "task artifacts were not a list"),
    ({"kind": "task", "status": {"state": "completed"}, "artifacts": ["boom"]},
     "task artifact was not an object"),
    ({"kind": "task", "status": {"state": "completed"},
      "artifacts": [{"parts": ["boom"]}]}, "reply part was not an object"),
    ({"kind": "message", "parts": "boom"}, "reply parts were not a list"),
    ({"kind": "message", "parts": ["boom"]}, "reply part was not an object"),
])
def test_the_terminal_wire_faults_on_a_wrongly_shaped_reply(result, needle):
    with pytest.raises(RuntimeError) as excinfo:
        _run_row_body({"jsonrpc": "2.0", "id": "1", "result": result})
    fault = excinfo.value
    assert needle in str(fault)
    assert getattr(fault, "_revl_transport_fault", False) is True


@pytest.mark.parametrize("result,needle", [
    ({"kind": "task", "status": "completed"}, "task status was not an object"),
    ({"kind": "message", "parts": ["boom"]}, "reply part was not an object"),
])
def test_on_failure_result_brings_a_wrongly_shaped_reply_back_in_band(result,
                                                                     needle):
    """The other half of the failure direction: under `on_failure(result)` the
    same malformed reply is the declared `Err`, not an exception that unwinds
    past the settlement the author opted into."""
    out, _calls = _run_row_body({"jsonrpc": "2.0", "id": "1", "result": result},
                                in_band=True)
    assert isinstance(out, _Err)
    assert needle in out.e


# -- the non-vacuity control, which passes with and without the gates ---------

def test_a_well_formed_reply_is_unchanged_by_the_shape_gates():
    """The control for the whole block. A COMPLIANT peer's Task walks
    `working` to `Done` with its payload intact, its `contextId` rides back on
    the handle, a direct `Message` still reads, and the terminal wire still
    answers — so the gates above refuse malformed replies and nothing else."""
    start = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "task", "id": "t-42", "contextId": "c-1",
        "status": {"state": "working"}}}
    out, _c, _Ev = _run_task_body("start", start, "survey the field")
    assert out == {"id": "t-42", "context": "c-1"}

    # a Task with NO contextId at all: the optional is genuinely optional
    bare = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "task", "id": "t-7", "status": {"state": "submitted"}}}
    out, _c, _Ev = _run_task_body("start", bare, "survey")
    assert out == {"id": "t-7", "context": None}

    working = _task(status={"state": "working"})
    out, _c, _Ev = _run_task_body("poll", working, {"id": "t-42", "context": None})
    assert out == _Ev("Status", "working")

    done = _task(status={"state": "completed"},
                 artifacts=[{"parts": [{"kind": "text", "text": "the "}]},
                            {"parts": [{"kind": "text", "text": "answer"}]}])
    out, _c, _Ev = _run_task_body("poll", done, {"id": "t-42", "context": None})
    assert out == _Ev("Done", "the answer")

    msg = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "message", "parts": [{"kind": "text", "text": "hello"}]}}
    out, _c, _Ev = _run_task_body("poll", msg, {"id": "t-42", "context": None})
    assert out == _Ev("Message", "hello")

    terminal = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "message", "parts": [{"kind": "text", "text": "pong"}]}}
    out, _calls = _run_row_body(terminal)
    assert out == "pong"


# ======================= `revl audit` over a COMPOSITION document (the CLI gap)
# Item 439 (issue #118). `docs/design/439-a2a-task-lifecycle.md` decision 3
# states G8 as "the boundary surface is the four (or one) synthesized externs,
# enumerable on the boundary surface `revl audit` renders, each carrying the
# folded `net.<host>` reach", and the two `_on_the_g8_audit_surface` tests above
# pin that over the composition's COMPILED DOCUMENT.
#
# The CLI did not reach that document. `revl audit` compiles its arguments as
# MODULES (`__main__`, `compile_files`), and a composition document declares no
# module-level component: its rows, and the provider a `remote` row SYNTHESIZES,
# exist only once the row table is resolved. So `revl audit base.rvl` printed
#
#     composition (providers first):
#
# and exited 0, for a composition that crosses to a peer over the network.
# `docs/design/439-a2a-transport-binding.md`'s scope limits named this as the
# gap between the property and the command.
#
# THE FAILURE DIRECTION, which is why it is a defect and not a missing feature:
# an audit surface is read to ENUMERATE authority, so an empty one is read as an
# ABSENCE of it. The silence failed OPEN — an operator counting what leaves the
# process saw zero crossings for a composition holding a `net.<host>` emission,
# with nothing on stderr and a zero exit to say the command had not looked. The
# composition is now resolved, and each shape the command cannot resolve refuses
# BY NAME with a nonzero exit rather than rendering an empty surface.

from revl.__main__ import main as _cli  # noqa: E402

MODULE = """
service Local {
  emission fn go(q: Str) -> Str
}
extern emission[net] fn ping(q: Str) -> Str = @py { return "" }
component LocalSvc provides local: Local {
  provide local {
    fn go(q) {
      let out = emit ping(q)
      return out
    }
  }
}
"""

LAYER_BASE = """
composition Net {
  use "services.rvl"
  stack "swap.rvl"
  remote @agent provides agent: Agent
    at host("agent.example:8443")
    through a2a
}
"""

LAYER = """
layer Swap for Net {
  touches key("agent")
}
"""


def _audit(tmp_path, *files, root=None):
    """`revl audit` over `files`, with the project root pinned at `tmp_path` so
    the row sources a composition names resolve where the fixture wrote them."""
    args = ["audit", *[str(tmp_path / f) for f in files],
            "--root", str(root or tmp_path)]
    return _cli(args)


def test_revl_audit_over_a_composition_renders_the_a2a_crossing(tmp_path, capsys):
    """The exit test for the CLI half of G8: `revl audit <composition>` renders
    the SYNTHESIZED crossing with its folded `net.<host>` reach.

    Before this slice the same invocation printed an empty surface and exited 0.
    """
    write(tmp_path, services=AGENT, base=WITHDRAW)
    assert _audit(tmp_path, "base.rvl") == 0
    out = capsys.readouterr().out
    assert "RemoteAgentProvider" in out
    assert "remote_agent_ask [net.agent_example]" in out
    # and the surface is not the empty one the module compile rendered
    assert out.splitlines()[0].strip() != "composition (providers first):"


def test_revl_audit_over_a_composition_carries_the_crossing_into_json(tmp_path,
                                                                     capsys):
    """The machine-readable surface, which is what a drift gate (`--diff`) and
    a policy gate (`--policy`) read, carries the same crossing."""
    write(tmp_path, services=AGENT, base=WITHDRAW)
    assert _audit(tmp_path, "base.rvl", ) == 0
    capsys.readouterr()
    assert _cli(["audit", str(tmp_path / "base.rvl"), "--root", str(tmp_path),
                 "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    provider = report["boundary"]["RemoteAgentProvider"]
    crossing, = provider["externs"]
    assert crossing["name"] == "remote_agent_ask"
    assert crossing["class"] == "emission"
    assert crossing["capabilities"] == ["net.agent_example"]


def test_revl_audit_over_the_four_op_composition_renders_all_four(tmp_path,
                                                                  capsys):
    """The `long_running` form: an operator counting what leaves the process
    reads four crossings from the CLI, the same four the compiled document
    carries."""
    _write_lr(tmp_path)
    assert _audit(tmp_path, "base.rvl") == 0
    out = capsys.readouterr().out
    for op in ("start", "poll", "reply", "cancel"):
        assert f"remote_researcher_research_{op} [net.agent_example]" in out


def test_revl_audit_refuses_a_composition_beside_modules(tmp_path, capsys):
    """A composition NAMES the rows it compiles, so auditing it beside
    hand-listed modules would render a surface neither describes. Refused by
    name, nonzero, rather than silently rendering one of the two."""
    write(tmp_path, services=AGENT, base=WITHDRAW, module=MODULE)
    assert _audit(tmp_path, "base.rvl", "module.rvl") == 1
    err = capsys.readouterr().err
    assert "base.rvl" in err and "module.rvl" in err
    assert "composition" in err


def test_revl_audit_refuses_two_composition_documents(tmp_path, capsys):
    """A composition document IS the audited unit, so two of them have no one
    boundary surface. Refused by name rather than rendering either."""
    write(tmp_path, services=AGENT, base=WITHDRAW,
          other=WITHDRAW.replace("composition Net", "composition Other"))
    assert _audit(tmp_path, "base.rvl", "other.rvl") == 1
    err = capsys.readouterr().err
    assert "2 composition documents" in err
    assert "base.rvl" in err and "other.rvl" in err


def test_revl_audit_refuses_a_layer_document(tmp_path, capsys):
    """A layer is a DELTA over a composition (426 §2.4), so it has no boundary
    surface of its own — and compiled as a module it renders the same empty one
    a composition used to. Refused by name, pointing at the composition that
    stacks it."""
    write(tmp_path, services=AGENT, base=LAYER_BASE, swap=LAYER)
    assert _audit(tmp_path, "swap.rvl") == 1
    err = capsys.readouterr().err
    assert "swap.rvl" in err
    assert "layer" in err and "DELTA" in err


def test_revl_audit_over_a_module_is_unchanged(tmp_path, capsys):
    """THE CONTROL, and it passes on both trees: a MODULE argument still
    compiles as a module and renders its own crossing. The composition door is
    an added branch, not a change to the one every other invocation takes."""
    write(tmp_path, module=MODULE)
    assert _audit(tmp_path, "module.rvl") == 0
    out = capsys.readouterr().out
    assert "LocalSvc" in out
    assert "ping [net]" in out


# ============ the REST of the shared compile group over a COMPOSITION document
# Item 439 (issue #118), the slice after G8c. `revl audit` was one command on a
# shared step: `main` compiles its file arguments as MODULES (`compile_files`)
# and hands the one `ir` to `test`, `query`, `erase-report`, `audit`, `goal`,
# `version` and `compile` alike. G8c fixed the document `audit` reads; the other
# commands still read the empty one, and answered from it.
#
# WHICH WAY EACH ONE FAILED, which is the whole of why three of them are defects
# and two are corrections:
#
#   compile       exit 0 and an IR document with `"services": {}`,
#                 `"components": []` and an empty `loadOrder`. A POSITIVE
#                 artifact asserting the composition contains nothing. FAIL-OPEN.
#   version       reads that same document. `--emit-manifest` prints it as the
#                 diff input a later `--against` reads, and a diff between two
#                 of them derives "PATCH — the interface is unchanged" for a
#                 composition that gained a whole remote provider and a
#                 `net.<host>` crossing. FAIL-OPEN, on a release gate.
#   test          collects nothing from the rows: "no tests to run", exit 0.
#                 A green by vacuity. FAIL-OPEN.
#   query         "unknown component: 'RemoteAgentProvider'" with an empty known
#                 list, exit 1. FAIL-CLOSED, but a false statement about a name
#                 the composition does define — corrected, not repaired.
#   erase-report  "unknown realm: 'books'", exit 1. FAIL-CLOSED, same shape.
#
# `goal` is deliberately left on the module step. `goal audit`'s zero exit over
# a composition with no termination contract is item 441/458's own decision
# (`docs/design/458-termination-language-surface.md` §2.1), and widening the
# document that decision is evaluated over is that item's call, not this one's.

GROUP_SERVICES = AGENT + """
service Ledger {
  emission fn write(line: Str) -> Int
}
"""

# One ordinary row's module, carrying the three things the rest of the group
# reads out of a compilation that a composition compiled as a module has none
# of: a component, a `lifecycle test`, and a realm.
GROUP_KIT = """
use "services.rvl" { }

component LocalLedger provides ledger: Ledger {
  isolate ledger in realm("books")
  let cells = effect Map.new() undo cells.drop()
  provide ledger {
    fn write(line) {
      effect cells.insert("last", line)
      undo   cells.remove("last")
      return 1
    }
  }
}

lifecycle test "the ledger reverts cleanly" {
  load LocalLedger
  let n = call ledger.write("a")
  assert n == 1
  unload LocalLedger
  assert no_residue
}
"""

GROUP = """
composition Net {
  use "services.rvl"
  row @ledger from "kit.rvl" provides ledger
  remote @agent provides agent: Agent
    at host("agent.example:8443")
    through a2a
}
"""


def _group(tmp_path, services: str = GROUP_SERVICES):
    """A composition with one ordinary row and one `through a2a` remote row."""
    write(tmp_path, services=services, kit=GROUP_KIT, base=GROUP)
    return tmp_path


def _run(tmp_path, *argv, doc="base"):
    return _cli([*argv, str(tmp_path / f"{doc}.rvl"), "--root", str(tmp_path)])


# ------------------------------------------------------------------ `compile`

def test_revl_compile_over_a_composition_writes_the_resolved_document(tmp_path,
                                                                      capsys):
    """`revl compile <composition>` writes the composition's OWN document.

    Before: an IR document with no services, no components and an empty load
    order, exit 0 — a positive artifact asserting the composition contains
    nothing, which is the fail-open direction for everything downstream of it.
    """
    _group(tmp_path)
    assert _run(tmp_path, "compile") == 0
    document = json.loads(capsys.readouterr().out)

    assert "Agent" in document["services"]
    names = [c["name"] for c in document["components"]]
    assert "LocalLedger" in names and "RemoteAgentProvider" in names
    assert document["manifest"]["loadOrder"]
    # and the synthesized crossing, with its folded reach, is IN the artifact
    crossing, = [e for e in document["externs"]
                 if e["name"] == "remote_agent_ask"]
    assert crossing["capabilities"] == ["net.agent_example"]


def test_revl_compile_over_a_module_is_unchanged(tmp_path, capsys):
    """THE CONTROL, green on both trees: a module argument still compiles as a
    module. The composition door is an added branch, not a change to the path
    every other invocation takes."""
    write(tmp_path, module=MODULE)
    assert _cli(["compile", str(tmp_path / "module.rvl")]) == 0
    document = json.loads(capsys.readouterr().out)
    assert [c["name"] for c in document["components"]] == ["LocalSvc"]


# ------------------------------------------------------------------ `version`

def test_revl_version_over_a_composition_measures_the_interface_change(tmp_path,
                                                                       capsys):
    """The release gate. Two generations of the same composition, one whose
    service gained a method, diffed through the CLI exactly as
    `docs/derived-versioning.md` documents (`--emit-manifest`, then `--against`).

    Before: both generations compiled to the same EMPTY document, so the
    verdict was "PATCH — the interface is unchanged" for a change that is not.
    """
    previous = tmp_path / "prev"
    previous.mkdir()
    _group(previous)
    assert _run(previous, "version", "--emit-manifest") == 0
    manifest = tmp_path / "prev.json"
    manifest.write_text(capsys.readouterr().out)

    now = tmp_path / "now"
    now.mkdir()
    grown = _group(now, services=GROUP_SERVICES.replace(
        "emission fn ask(question: Str) -> Str",
        "emission fn ask(question: Str) -> Str\n"
        "  emission fn echo(line: Str) -> Str"))
    assert _cli(["version", str(grown / "base.rvl"), "--root", str(grown),
                 "--against", str(manifest), "--json"]) == 0
    verdict = json.loads(capsys.readouterr().out)

    assert verdict["bump"] == "minor"
    added, = [c for c in verdict["changes"] if c["method"] == "echo"]
    assert added["service"] == "Agent"


# --------------------------------------------------------------------- `test`

def test_revl_test_over_a_composition_collects_the_rows_tests(tmp_path, capsys):
    """`revl test --list <composition>` collects the `lifecycle test` a ROW's
    module declares.

    Before: "no tests to run", exit 0 — a green by vacuity, which is the worst
    direction a test command can fail in.
    """
    _group(tmp_path)
    assert _run(tmp_path, "test", "--list") == 0
    out = capsys.readouterr().out
    assert "the ledger reverts cleanly" in out
    assert "1 test(s) collected" in out


# -------------------------------------------------------------------- `query`

def test_revl_query_over_a_composition_answers_for_the_synthesized_provider(
        tmp_path, capsys):
    """`revl query reaches` over a composition reaches the SYNTHESIZED provider.

    Before: exit 1 with "unknown component: 'RemoteAgentProvider'" and an empty
    known list — fail-closed, but a false statement about a name the
    composition does define.
    """
    _group(tmp_path)
    assert _run(tmp_path, "query", "reaches", "RemoteAgentProvider",
                "--json") == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["ok"] is True
    assert answer["component"] == "RemoteAgentProvider"
    emitted = {e.get("name") for e in answer["surface"]["emissions"]}
    assert "remote_agent_ask" in emitted


# ------------------------------------------------------------- `erase-report`

def test_revl_erase_report_over_a_composition_finds_the_rows_realm(tmp_path,
                                                                   capsys):
    """`revl erase-report --realm books` reports on the realm a ROW's module
    declares, and names the remote provider among the survivors.

    Before: exit 1 with "unknown realm: 'books'" — fail-closed, and false.
    """
    _group(tmp_path)
    assert _run(tmp_path, "erase-report", "--realm", "books",
                "--no-residue-proof", "--json") == 0
    document = json.loads(capsys.readouterr().out)
    assert document["realm"] == "books"
    assert document["otherRealmsUntouched"]["untouched"] is True
    assert "RemoteAgentProvider" in document["otherRealmsUntouched"]["survivors"]


# --------------------------------------------- the refusals, on every command

@pytest.mark.parametrize("command", ["compile", "version", "test",
                                     "erase-report"])
def test_every_command_on_the_door_refuses_a_layer_document(tmp_path, capsys,
                                                            command):
    """A layer is a DELTA over a composition (426 §2.4), so it has no
    composition of its own to resolve — and compiled as a MODULE it yields the
    same empty document a composition did. Each command refuses by name,
    nonzero, rather than answering from the empty one."""
    write(tmp_path, services=AGENT, base=LAYER_BASE, swap=LAYER)
    extra = ["--realm", "books"] if command == "erase-report" else []
    assert _cli([command, str(tmp_path / "swap.rvl"), "--root", str(tmp_path),
                 *extra]) == 1
    err = capsys.readouterr().err
    assert f"`revl {command}`" in err
    assert "swap.rvl" in err
    assert "layer" in err and "DELTA" in err


def test_revl_compile_refuses_a_composition_beside_modules(tmp_path, capsys):
    """A composition NAMES the rows it compiles, so reading it beside
    hand-listed modules would answer for a program neither describes."""
    write(tmp_path, services=AGENT, base=WITHDRAW, module=MODULE)
    assert _cli(["compile", str(tmp_path / "base.rvl"),
                 str(tmp_path / "module.rvl"), "--root", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "`revl compile`" in err
    assert "base.rvl" in err and "module.rvl" in err


def test_revl_compile_refuses_two_composition_documents(tmp_path, capsys):
    """A composition document IS the compiled unit, so two of them have no one
    document to answer over."""
    write(tmp_path, services=AGENT, base=WITHDRAW,
          other=WITHDRAW.replace("composition Net", "composition Other"))
    assert _cli(["compile", str(tmp_path / "base.rvl"),
                 str(tmp_path / "other.rvl"), "--root", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "2 composition documents" in err


# ------------------------------------------------------------ `goal` is LEFT
def test_revl_goal_audit_over_a_composition_is_deliberately_untouched(tmp_path,
                                                                      capsys):
    """`goal audit` stays on the MODULE step, and this test pins that on
    purpose so a later reader knows the omission is a decision.

    Its zero exit over a composition with no termination contract is item
    441/458's, stated in `docs/design/458-termination-language-surface.md`
    §2.1. Routing it through this door would change the document that decision
    is evaluated over without the review that belongs to it, so it is the one
    command on the shared step this slice does not move.
    """
    _group(tmp_path)
    assert _cli(["goal", "audit", str(tmp_path / "base.rvl")]) == 0
    assert "no goal service" in capsys.readouterr().out


# ===================== the item-33 BOUNDARY POLICY over a COMPOSITION document
# Item 439 (issue #118), slice G8e. G8c put `revl audit` on the composition
# door and G8d put the rest of its shared compile step there. The item-33
# boundary policy has THREE more doors, and none of them is on that step:
#
#   * `revl policy evaluate` (item 290) — the DRY RUN of the same
#     `policy.evaluate` the gate calls, one comparison site by construction;
#   * `revl dash --policy` (item 63) — the policy-exception queue a supervisor
#     rules on;
#   * `revl simulate policy-diff --composition` (item 468) — the realms a
#     realm-scoped rule decides by.
#
# Each compiled its `.rvl` arguments as MODULES, so each read an empty audit
# graph for a composition document and answered from it.
#
# WHICH WAY EACH ONE FAILED. `policy evaluate` printed "gate verdict: clean —
# every selected component clears its thresholds" and exited 0 for the very
# composition `revl audit --policy` refuses with a named violation: a POSITIVE
# verdict, on the preview of a gate, disagreeing with the gate it previews.
# That is fail-OPEN, and it is worse than G8c's empty surface, because an empty
# surface at least renders nothing while this one asserts cleanliness.
# `dash --policy` printed "nothing pending — no widening to ack, no policy
# exception to rule on" for the same composition: an empty decision queue is
# read as an absence of decisions, so it failed OPEN too. `simulate
# policy-diff --composition` resolved NO realms out of the document the
# operator handed it, so every action a realm-scoped rule selects stayed
# undecided — the refusing direction, and so a correction rather than a repair,
# but still a false statement about a document that DOES name its rows' realms.

DENY_NET = "component * may not reach net*\n"
ALLOW_NET = "component * may reach net*\n"

REALM_ROW = """
composition Net {
  use "services.rvl"
  remote @agent provides agent: Agent
    in realm("tenant_a")
    at host("agent.example:8443")
    through a2a
}
"""


def _policy_file(tmp_path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _evaluate(tmp_path, policy: str, *files: str, root: bool = True) -> int:
    """`revl policy evaluate POLICY FILES...`, with the project root pinned at
    `tmp_path` so the row sources a composition names resolve where the fixture
    wrote them. `root=False` leaves `--root` off, which is what the module
    CONTROLS take: `--root` is an argument this slice adds, so a control that
    passed it could not be run on the predecessor tree at all."""
    argv = ["policy", "evaluate", policy, *[str(tmp_path / f) for f in files]]
    if root:
        argv += ["--root", str(tmp_path)]
    return _cli(argv)


def _dash(tmp_path, *files: str, policy: str | None = None,
          as_json: bool = True, root: bool = True) -> int:
    argv = ["dash", *[str(tmp_path / f) for f in files]]
    if root:
        argv += ["--root", str(tmp_path)]
    if policy is not None:
        argv += ["--policy", policy]
    if as_json:
        argv.append("--json")
    return _cli(argv)


# ------------------------------------------------------- `revl policy evaluate`

def test_revl_policy_evaluate_over_a_composition_refuses_the_a2a_crossing(
        tmp_path, capsys):
    """THE EXIT TEST for the dry run. The synthesized `remote ... through a2a`
    provider reaches `net.agent_example`, a deny rule names it, and the dry run
    now reports the component that would be refused.

    Before this slice the same invocation printed `clean` and exited 0.
    """
    write(tmp_path, services=AGENT, base=WITHDRAW)
    policy = _policy_file(tmp_path, "deny.policy", DENY_NET)
    assert _evaluate(tmp_path, policy, "base.rvl") == 1
    out = capsys.readouterr().out
    assert "RemoteAgentProvider" in out
    assert "would be REFUSED" in out
    assert "gate verdict: REFUSED" in out


def test_revl_policy_evaluate_agrees_with_the_gate_it_previews(tmp_path,
                                                               capsys):
    """The invariant item 290 states — the dry run and the gate run the SAME
    `policy.evaluate`, one comparison site — held for a module and was FALSE
    for a composition document. It is the disagreement, not the exit status
    alone, that made the silence fail open: an operator previewing a gate reads
    the preview as what the gate will do.

    Both directions are pinned, so a door that resolved the document but
    answered from a different graph would still be caught.
    """
    write(tmp_path, services=AGENT, base=WITHDRAW)
    deny = _policy_file(tmp_path, "deny.policy", DENY_NET)
    allow = _policy_file(tmp_path, "allow.policy", ALLOW_NET)
    for policy, verdict in ((deny, 1), (allow, 0)):
        assert _cli(["audit", str(tmp_path / "base.rvl"), "--root",
                     str(tmp_path), "--policy", policy]) == verdict
        capsys.readouterr()
        assert _evaluate(tmp_path, policy, "base.rvl") == verdict
        capsys.readouterr()


def test_revl_policy_evaluate_refuses_a_layer_document(tmp_path, capsys):
    """A layer is a DELTA (426 §2.4): no composition of its own, and compiled
    as a module the same empty graph. Refused by name.

    The status is 2, not 1, and deliberately: on this verb 1 already MEANS "a
    component would be refused", so returning 1 for a document it could not
    read would be indistinguishable from a policy verdict it never reached.
    """
    write(tmp_path, services=AGENT, base=LAYER_BASE, swap=LAYER)
    policy = _policy_file(tmp_path, "deny.policy", DENY_NET)
    assert _evaluate(tmp_path, policy, "swap.rvl") == 2
    err = capsys.readouterr().err
    assert "`revl policy evaluate`" in err
    assert "swap.rvl" in err
    assert "layer" in err and "DELTA" in err


def test_revl_policy_evaluate_refuses_a_composition_beside_modules(tmp_path,
                                                                   capsys):
    """A composition names the rows it compiles, so evaluating it beside
    hand-listed modules would report on a program neither describes."""
    write(tmp_path, services=AGENT, base=WITHDRAW, module=MODULE)
    policy = _policy_file(tmp_path, "deny.policy", DENY_NET)
    assert _evaluate(tmp_path, policy, "base.rvl", "module.rvl") == 2
    err = capsys.readouterr().err
    assert "`revl policy evaluate`" in err
    assert "base.rvl" in err and "module.rvl" in err


def test_revl_policy_evaluate_refuses_two_composition_documents(tmp_path,
                                                                capsys):
    """A composition document IS the evaluated unit."""
    write(tmp_path, services=AGENT, base=WITHDRAW,
          other=WITHDRAW.replace("composition Net", "composition Other"))
    policy = _policy_file(tmp_path, "deny.policy", DENY_NET)
    assert _evaluate(tmp_path, policy, "base.rvl", "other.rvl") == 2
    assert "2 composition documents" in capsys.readouterr().err


def test_revl_policy_evaluate_over_a_module_is_unchanged(tmp_path, capsys):
    """THE CONTROL, green on both trees: a MODULE argument still compiles as a
    module, and the verb still reports its reaching component. The composition
    door is an added branch, not a change to the path every other invocation
    takes."""
    write(tmp_path, module=MODULE)
    policy = _policy_file(tmp_path, "deny.policy", DENY_NET)
    assert _evaluate(tmp_path, policy, "module.rvl", root=False) == 1
    out = capsys.readouterr().out
    assert "LocalSvc" in out
    assert "would be REFUSED" in out


# ----------------------------------------------------------- `revl dash --policy`

def test_revl_dash_over_a_composition_queues_the_policy_exception(tmp_path,
                                                                  capsys):
    """THE EXIT TEST for the supervisor's queue: the violation the synthesized
    provider raises is a pending decision with its why-trace attached.

    Before this slice the queue was empty and the pane said there was nothing
    to rule on.
    """
    write(tmp_path, services=AGENT, base=WITHDRAW)
    policy = _policy_file(tmp_path, "deny.policy", DENY_NET)
    assert _dash(tmp_path, "base.rvl", policy=policy) == 0
    snapshot = json.loads(capsys.readouterr().out)
    exception, = snapshot["decisions"]["policy"]
    assert exception["component"] == "RemoteAgentProvider"
    assert exception["token"] == "net.agent_example"
    assert exception["why"]["steps"][0]["detail"].endswith("`remote_agent_ask`")


def test_revl_dash_refuses_a_layer_document(tmp_path, capsys):
    """The same three shapes refuse here, with `dash`'s own nonzero status."""
    write(tmp_path, services=AGENT, base=LAYER_BASE, swap=LAYER)
    assert _dash(tmp_path, "swap.rvl") == 1
    err = capsys.readouterr().err
    assert "`revl dash`" in err
    assert "layer" in err and "DELTA" in err


def test_revl_dash_over_a_module_is_unchanged(tmp_path, capsys):
    """THE CONTROL, green on both trees."""
    write(tmp_path, module=MODULE)
    policy = _policy_file(tmp_path, "deny.policy", DENY_NET)
    assert _dash(tmp_path, "module.rvl", policy=policy, root=False) == 0
    snapshot = json.loads(capsys.readouterr().out)
    exception, = snapshot["decisions"]["policy"]
    assert exception["component"] == "LocalSvc"


# ------------------------------------------ `revl simulate policy-diff`

def _wal(tmp_path, component: str, token: str) -> str:
    """One WAL holding one recorded emission for `component`, written by the py
    in-process driver — the same builder `test_468_policy_diff.py` uses."""
    sys.path.insert(0, str(ROOT / "backends" / "python"))
    import replay  # noqa: PLC0415 — the py backend runtime, not a revl module

    path = str(tmp_path / "run.wal")
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    timeline = replay.Timeline(component)
    step = replay.Step(0, replay.KIND_EMISSION, "agent.ask", None,
                       {"phase": "activation"}, detail=None)
    step.scope = {"caps": [token]}
    timeline.steps.append(step)
    wal.append_timeline(timeline)
    wal.commit_activation(components=[component])
    wal.close()
    return path


def _policy_diff(tmp_path, old: str, new: str, history: str,
                 *composition: str) -> int:
    argv = ["simulate", "policy-diff", old, new, "--history", history]
    for doc in composition:
        argv += ["--composition", str(tmp_path / doc)]
    return _cli([*argv, "--root", str(tmp_path)])


def test_revl_simulate_policy_diff_resolves_the_rows_realms(tmp_path, capsys):
    """THE EXIT TEST for the realm resolution: a `remote ... through a2a` row
    isolated into `realm("tenant_a")` is the realm a realm-scoped rule decides
    the recorded crossing by, and the composition document is the only place
    that fact lives.

    Without `--composition` the crossing is UNDECIDED and the verb exits 1,
    which is the honest answer and stays. Handed the composition document it
    used to give the SAME answer — the operator supplied the fact and the
    command dropped it. Now the rule decides.
    """
    write(tmp_path, services=AGENT, base=REALM_ROW)
    history = _wal(tmp_path, "RemoteAgentProvider", "net.agent_example")
    old = _policy_file(tmp_path, "loose.policy",
                       "realm tenant_a may reach net.agent_example\n")
    new = _policy_file(tmp_path, "tight.policy",
                       "realm tenant_a may not reach net.agent_example\n")

    assert _policy_diff(tmp_path, old, new, history) == 1
    assert "UNDECIDED" in capsys.readouterr().out

    assert _policy_diff(tmp_path, old, new, history, "base.rvl") == 0
    out = capsys.readouterr().out
    assert "NEWLY DENIED:" in out
    assert "RemoteAgentProvider net.agent_example" in out
    assert "0 undecided" in out


def test_revl_simulate_policy_diff_refuses_a_layer_document(tmp_path, capsys):
    """`--composition` takes the same document the other doors take, so it
    refuses the same three shapes, with this verb's usage status."""
    write(tmp_path, services=AGENT, base=LAYER_BASE, swap=LAYER)
    history = _wal(tmp_path, "RemoteAgentProvider", "net.agent_example")
    old = _policy_file(tmp_path, "loose.policy",
                       "realm tenant_a may reach net.agent_example\n")
    new = _policy_file(tmp_path, "tight.policy",
                       "realm tenant_a may not reach net.agent_example\n")
    assert _policy_diff(tmp_path, old, new, history, "swap.rvl") == 2
    err = capsys.readouterr().err
    assert "`revl simulate policy-diff`" in err
    assert "layer" in err and "DELTA" in err

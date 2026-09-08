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


# ---------------------------------------------- the generated crossing, executed

class _Ok:
    def __init__(self, v):
        self.v = v


class _Err:
    def __init__(self, e):
        self.e = e


def _run_row_body(reply, *, in_modality="text", out_modality="text",
                  in_band=False, rest=False, status=200, arg="ping"):
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
            return _Resp(json.dumps(reply).encode())

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


def test_long_running_over_rest_is_refused(tmp_path):
    base = LR.replace("through a2a\n", "through a2a_rest\n")
    _write_lr(tmp_path, base=base)
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    assert "a2a_rest" in str(excinfo.value)


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


def _run_task_body(kind, reply, *args, status=200, label="researcher"):
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
    assert fault.revl_row == "researcher"
    assert fault.revl_crossing == f"research_{kind}"

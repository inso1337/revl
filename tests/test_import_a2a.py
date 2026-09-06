"""`revl import a2a` — an A2A 1.0.0 Agent Card -> revl source.

Roadmap item 439, slice 1. Four claims are under test.

The first is the family's, mechanically: **the generated source compiles**.
Every fixture is round-tripped through `compile_source` and its IR inspected,
the same shape as `test_import_wit.py`, `test_import_openapi.py` and
`test_import_cordis.py`.

The second is the teardown answer, made executable rather than asserted in
prose: **no inverse is ever synthesized**. A remote A2A call cannot be a
`bracket` (an inverse over a network is not infallible, so it cannot carry G5)
and cannot be `transactional` (item 243 wants a HOST-LOCAL inverse and a
witness, and a peer's witness is one more claim by the peer). A2A's
`tasks/cancel` is not an inverse either. So every operation is `emission` with
G4's `emit` marker and nothing else, and the tests below pin that no `undo`, no
`compensate` and no `acquire` slot ever appears on a generated extern.

The third is that the Agent Card is a CLAIM (item 439 decision (2)): every
result is `Untrusted[Str]`, and the checker — not this importer — is what
refuses a card result reaching an authority-granting sink.

The fourth is the slice boundary itself, pinned so it cannot erode: version
honesty (`1.0.0` exactly), the two JSON-body transports A2A 1.0.0 defines
(JSON-RPC 2.0 and HTTP+JSON/REST) bound and gRPC refused, text modalities only,
and a non-terminal Task refused at the boundary instead of polled.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.import_a2a import import_a2a, import_a2a_file  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "a2a"
BILLING = str(FIXTURES / "billing.json")
LOCAL = str(FIXTURES / "local.json")


def _card(**overrides) -> dict:
    doc = json.loads(Path(BILLING).read_text(encoding="utf-8"))
    doc.update(overrides)
    return doc


def _refusal(doc, **kwargs) -> str:
    with pytest.raises(RevlError) as excinfo:
        import_a2a(doc, filename="card.json", **kwargs)
    return str(excinfo.value)


# ------------------------------------------------- 1. the generated source works

def test_agent_card_becomes_a_service_and_a_provider():
    source = import_a2a_file(BILLING)
    assert "service BillingAgent {" in source
    assert "component BillingAgentProvider provides billing_agent: BillingAgent {" in source
    # the trust direction is stated, not implied
    assert "THE AGENT CARD IS A CLAIM, NOT A SPECIFICATION." in source
    assert "TRUSTED, NOT CHECKED (G8)" in source


@pytest.mark.parametrize("backend", ["ts", "py"])
def test_generated_source_compiles(backend):
    ir = compile_source(import_a2a_file(BILLING, backend=backend), "billing.rvl")
    methods = ir["services"]["BillingAgent"]["methods"]
    # one operation per skill, named after the skill id, text in / text out
    assert sorted(methods) == ["invoice_lookup", "issue_refund"]
    assert methods["invoice_lookup"]["params"] == [{"name": "message", "type": "Str"}]
    assert methods["invoice_lookup"]["emission"] is True
    assert [c["name"] for c in ir["components"]] == ["BillingAgentProvider"]
    assert ir["components"][0]["provides"] == {"billing_agent": "BillingAgent"}
    assert all(backend in extern["bodies"] for extern in ir["externs"])


# ------------------------------------------- 2. the teardown answer, executable

def test_no_inverse_is_ever_synthesized():
    """The load-bearing claim of this slice. A remote effect has no local undo,
    so every crossing is an `emission` carrying G4's `emit` marker and nothing
    else: no `bracket` (an acquire's release must be infallible, G5), no
    `transactional` (item 243 needs a host-local inverse), no auto-attached
    `compensate`."""
    ir = compile_source(import_a2a_file(BILLING), "billing.rvl")
    assert ir["externs"], "the fixture generates externs"
    for extern in ir["externs"]:
        assert extern["class"] == "emission"
        # no inverse of any grade is attached to a generated crossing
        assert not extern.get("undo")
        assert not extern.get("compensate")
        assert not extern.get("witness")


def test_no_generated_extern_is_an_acquire():
    source = import_a2a_file(BILLING)
    for line in source.splitlines():
        if line.startswith("extern "):
            assert line.startswith("extern emission["), line


def test_teardown_reasoning_is_stated_in_the_generated_file():
    """A refusal nobody can read is a refusal nobody can act on, so the reason
    ships with the code rather than only in the docs."""
    source = import_a2a_file(BILLING)
    assert "A REMOTE A2A CALL CANNOT PARTICIPATE IN G7" in source
    assert "`tasks/cancel` is not an inverse" in source
    assert "the remote effect STAYS" in source
    # the one kind an A2A call CAN carry is named, and named as hand-written
    assert "`compensate` BY HAND" in source


def test_teardown_never_calls_tasks_cancel():
    """`tasks/cancel` asks a RUNNING task to stop, the agent may refuse it, and
    it says nothing about a task already terminal. It is not an unwind path."""
    for backend in ("ts", "py"):
        source = import_a2a_file(BILLING, backend=backend)
        body_lines = [line for line in source.splitlines()
                      if not line.lstrip().startswith(("//", "#"))]
        assert not any("tasks/cancel" in line for line in body_lines)


# --------------------------------------- 3. the card is a claim: taint (D-424c.9)

def test_every_result_is_untrusted():
    source = import_a2a_file(BILLING)
    assert "-> Untrusted[Str]" in source
    assert "fn invoice_lookup(message: Str) -> Untrusted[Str]" in source


def test_a_card_result_cannot_reach_an_authority_sink():
    """The exit test for D-424c.9, and it is the CHECKER that enforces it: a
    generated client looks exactly like a local provider at every call site, so
    the taint qualifier is what keeps a remote value from reaching an outbound
    send invisibly.

    The consumer is `async` because the ts binding's operation is (issue #251):
    a sync consumer is refused by the async-propagation rule (A1) BEFORE the
    body is taint-checked, which would leave this exit test passing on the
    wrong refusal."""
    consumer = """
service Shell {
  emission async fn go() -> Str
}

extern emission[shell] fn run_cmd(cmd: Trusted[Str]) -> Str
  = @ts { throw new Error("stub"); }

component Shell requires billing_agent: BillingAgent provides shell: Shell {
  provide shell {
    async fn go() {
      let answer = emit billing_agent.invoice_lookup("INV-1")
      let out = emit run_cmd(answer)
      return out
    }
  }
}
"""
    source = import_a2a_file(BILLING) + consumer
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, "billing.rvl")
    message = str(excinfo.value)
    assert "untrusted value" in message
    assert "G9" in message


def test_no_verified_remote_badge():
    """Item 424 D-424c.8: a client sits on the SENDING side and holds no gate
    over the callee, so importing a card must not read as admitting an agent."""
    source = import_a2a_file(BILLING)
    assert "THIS FILE MAKES NO CLAIM ABOUT WHAT THE PEER RUNS." in source
    assert "does not admit, verify or re-admit the agent" in source


# ------------------------------------------------ 4. the slice boundary, pinned

def test_version_honesty_the_claim_is_exact():
    """Item 439 decision (3): the claim is "A2A 1.0.0", never "A2A"."""
    assert "Protocol: A2A 1.0.0 over JSON-RPC 2.0" in import_a2a_file(BILLING)


@pytest.mark.parametrize("claimed", ["0.3.0", "1.0", "1.1.0", "", 1.0, None])
def test_another_protocol_version_is_refused(claimed):
    doc = _card()
    if claimed is None:
        doc.pop("protocolVersion")
    else:
        doc["protocolVersion"] = claimed
    message = _refusal(doc)
    assert "#/protocolVersion" in message
    assert "1.0.0" in message


def test_grpc_is_refused_naming_the_transport():
    """gRPC is a binary transport over HTTP/2 with protobuf framing, not the
    POST-a-JSON-body crossing this generator emits, so it is refused rather than
    approximated. JSON-RPC and HTTP+JSON are the two that ARE bound."""
    message = _refusal(_card(preferredTransport="GRPC"))
    assert "GRPC" in message
    assert "gRPC" in message
    assert "#/preferredTransport" in message


@pytest.mark.parametrize("backend", ["ts", "py"])
def test_httpjson_transport_binds_a_rest_crossing(backend):
    """The HTTP+JSON/REST transport is bound as a second single crossing: the
    same revl surface (service, extern, provider; text in / text out;
    `Untrusted[Str]`), differing only in the wire — a POST to
    `<endpoint>/v1/message:send` with a bare `{ message }` body, no JSON-RPC
    envelope."""
    doc = _card(preferredTransport="HTTP+JSON")
    source = import_a2a(doc, filename="card.json", backend=backend)
    # same revl surface as the JSON-RPC binding
    ir = compile_source(source, "billing.rvl")
    methods = ir["services"]["BillingAgent"]["methods"]
    assert sorted(methods) == ["invoice_lookup", "issue_refund"]
    assert methods["invoice_lookup"]["params"] == [{"name": "message", "type": "Str"}]
    assert methods["invoice_lookup"]["emission"] is True
    assert "fn invoice_lookup(message: Str) -> Untrusted[Str]" in source
    # the REST method path is where the crossing posts
    assert "/a2a/v1/message:send" in source
    # the JSON-RPC envelope is gone: no `jsonrpc` wrapper and no `message/send`
    # (the ts `fetch` still carries `method: "POST"`, which is the HTTP method,
    # not the JSON-RPC `method` member)
    assert "jsonrpc" not in source
    assert "message/send" not in source
    # the header names the transport it actually speaks
    assert "over HTTP+JSON/REST (`POST /v1/message:send`)" in source
    assert "Protocol: A2A 1.0.0 over JSON-RPC" not in source


@pytest.mark.parametrize("backend", ["ts", "py"])
def test_httpjson_keeps_every_boundary_guarantee(backend):
    """Only the wire changed. The redirect refusal, the time bound, the
    terminal-state discipline, the no-inverse teardown and the taint are all
    exactly the JSON-RPC binding's."""
    doc = _card(preferredTransport="HTTP+JSON")
    source = import_a2a(doc, filename="card.json", backend=backend)
    assert "redirect refused" in source
    assert "non-terminal state" in source and "does not poll" in source
    assert "-> Untrusted[Str]" in source
    # every generated extern is `emission` with no inverse of any grade, exactly
    # as the JSON-RPC binding (checked on the declarations, not the header prose)
    ir = compile_source(source, "billing.rvl")
    for extern in ir["externs"]:
        assert extern["class"] == "emission"
        assert not extern.get("undo") and not extern.get("compensate")
    # a REST error is a non-2xx status, and it is a FAULT like any transport
    # failure, never a quietly-empty result
    assert "transport failure" in source


def test_httpjson_additional_interfaces_note_names_the_bound_transport():
    """When HTTP+JSON is bound, the JSON-RPC and gRPC interfaces the card also
    advertises are recorded as NOT PROJECTED, and the note says the file speaks
    HTTP+JSON/REST to `url` only."""
    doc = _card(preferredTransport="HTTP+JSON")
    source = import_a2a(doc, filename="card.json")
    assert "this file speaks HTTP+JSON/REST to `url` only" in source
    # the other advertised transports are named, the bound one is not duplicated
    assert "GRPC" in source
    assert "JSONRPC" in source


def test_streaming_is_recorded_not_projected():
    """The load-bearing OPEN question of item 439 (one emission? a stream, item
    130? a session, item 250?) is neither answered nor pre-empted here: the
    capability is surfaced as unbound rather than silently dropped."""
    source = import_a2a_file(BILLING)
    assert "NOT PROJECTED" in source
    assert "`capabilities.streaming`" in source
    assert "`capabilities.pushNotifications`" in source
    assert "item 439's open question" in source
    # and nothing streaming-shaped is actually generated
    assert "message/stream" not in source.replace(
        "`message/stream` and", "")


def test_an_optional_extension_is_recorded_not_projected():
    """An optional extension is surfaced as unbound (like streaming), not
    silently dropped and not refused: it does not change what the boundary must
    honour."""
    doc = _card()
    doc["capabilities"] = dict(doc["capabilities"])
    doc["capabilities"]["extensions"] = [
        {"uri": "https://ext.example/trace", "required": False},
        {"uri": "https://ext.example/hints"},
    ]
    source = import_a2a(doc, filename="card.json")
    assert "NOT" in source and "projected" in source
    assert "`capabilities.extensions`" in source
    assert "https://ext.example/trace" in source
    assert "https://ext.example/hints" in source


def test_a_required_extension_is_refused_not_recorded():
    """A2A 1.0.0's `required` means the client must comply to interact. This
    slice projects no extension, so a mandatory one is refused (naming it and
    its pointer) rather than recorded as if the plain provider satisfied it —
    the honesty rule the version and transport checks already enforce."""
    doc = _card()
    doc["capabilities"] = dict(doc["capabilities"])
    doc["capabilities"]["extensions"] = [
        {"uri": "https://ext.example/must", "required": True},
    ]
    message = _refusal(doc)
    assert "REQUIRED" in message
    assert "https://ext.example/must" in message
    assert "#/capabilities/extensions/0" in message


def test_a_required_extension_without_a_uri_is_still_refused():
    """A required extension the client cannot even identify is no less binding;
    it is refused by index rather than let through."""
    doc = _card()
    doc["capabilities"] = dict(doc["capabilities"])
    doc["capabilities"]["extensions"] = [{"required": True}]
    message = _refusal(doc)
    assert "REQUIRED" in message
    assert "index 0" in message
    assert "#/capabilities/extensions/0" in message


def test_a_non_terminal_task_is_a_fault_not_a_poll():
    for backend in ("ts", "py"):
        source = import_a2a_file(BILLING, backend=backend)
        assert "non-terminal state" in source
        assert "does not poll" in source
        # nothing in this slice fetches a task by id or resubscribes
        assert "tasks/get" not in source
        assert "tasks/resubscribe" not in source.replace(
            "`tasks/resubscribe` are NOT projected", "")


def test_a_non_text_modality_is_refused():
    doc = _card()
    doc["skills"][0]["outputModes"] = ["application/pdf"]
    message = _refusal(doc)
    assert "application/pdf" in message
    assert "invoice-lookup" in message
    assert "#/skills/0/outputModes" in message


def test_a_card_with_no_skills_is_refused():
    message = _refusal(_card(skills=[]))
    assert "advertises no skills" in message


@pytest.mark.parametrize("bad_id", ["123", "", "  ", "fn", "-"])
def test_an_unusable_skill_id_is_refused_not_repaired(bad_id):
    doc = _card()
    doc["skills"][0]["id"] = bad_id
    message = _refusal(doc)
    assert "#/skills/0/id" in message


def test_two_skills_folding_onto_one_operation_are_refused():
    doc = _card()
    doc["skills"][1]["id"] = "invoice_lookup"
    message = _refusal(doc)
    assert "collides" in message
    assert "#/skills/1/id" in message


# ------------------------------------------- the reach bound (items 421 F4, 424 D-424c.10)

def test_reach_is_derived_from_the_host():
    ir = compile_source(import_a2a_file(BILLING), "billing.rvl")
    for extern in ir["externs"]:
        assert extern["capabilities"] == ["net.billing_internal"]


def test_a_credential_in_the_url_never_reaches_the_generated_source():
    """Item 421 F4: a URL credential is a live secret, not an identifier. It
    must appear in no comment, no host body and no capability spelling."""
    doc = _card(url="https://alice:s3cr3t@billing.internal:8443/a2a")
    for backend in ("ts", "py"):
        source = import_a2a(doc, filename="card.json", backend=backend)
        assert "s3cr3t" not in source
        assert "alice" not in source
        assert "net.billing_internal" in source
        assert "// Endpoint: https://billing.internal:8443/a2a" in source
        assert "REDACTED" in source
        compile_source(source, "billing.rvl")


def test_a_url_outside_the_character_class_is_refused():
    """The endpoint is interpolated into a `//` comment and into a host body,
    so it is held to a strict shape up front. Item 416f is the same hole found
    in the sibling importer; here it is closed by refusing rather than by
    escaping after the fact."""
    for bad in ('https://x/"+1+"', "https://x/}", "https://x/\\", "ftp://x/",
                "https://x/ y", "not a url"):
        message = _refusal(_card(url=bad))
        assert "#/url" in message
        # the refusal does not echo the offending URL back
        assert bad not in message


def test_plaintext_http_is_refused_and_opt_in_is_recorded():
    message = _refusal(_card(url="http://127.0.0.1:9999/a2a"))
    assert "plaintext" in message
    assert "--allow-plaintext" in message
    source = import_a2a_file(LOCAL, allow_plaintext=True)
    ir = compile_source(source, "local.rvl")
    assert "// Endpoint: http://127.0.0.1:9999/a2a" in source
    # a bare IP folds to a token that would otherwise lex as a NUMBER with
    # digit separators, so the reach token is prefixed rather than broken
    assert ir["externs"][0]["capabilities"] == ["net.h_127_0_0_1"]


# --------------------------------------------------- codegen injection (item 416f)

def test_card_text_cannot_inject_declarations():
    """Every card-derived string reaching a `//` comment goes through the
    audited `_comment_safe`, so a newline in the agent's own text cannot end
    the comment and drop the rest into compiled source."""
    doc = _card(
        name="Bad\ncomponent Evil provides evil: BillingAgent {\n",
        description='x\nextern emission fn pwn() -> Unit = @ts { 1; }\n',
    )
    doc["skills"][0]["name"] = "y\ncomponent AlsoEvil provides e2: BillingAgent {\n"
    doc["skills"][0]["tags"] = ["t\ncomponent Third provides e3: BillingAgent {\n"]
    ir = compile_source(import_a2a(doc, filename="card.json"), "card.rvl")
    assert [c["name"] for c in ir["components"]] == [
        "BadComponentEvilProvidesEvilBillingAgentProvider"]
    assert all(extern["name"].startswith("a2a_") for extern in ir["externs"])
    assert "pwn" not in {extern["name"] for extern in ir["externs"]}


def test_a_security_scheme_generates_no_credential():
    source = import_a2a_file(BILLING)
    assert "the card declares a security scheme" in source
    assert "capability-bound secret" in source
    assert "Authorization" not in source
    assert "Bearer" not in source


# ------------------------------------------------------------------------ CLI

def test_cli_writes_generated_source(tmp_path, capsys):
    out = tmp_path / "billing.rvl"
    assert main(["import", "a2a", BILLING, "-o", str(out)]) == 0
    compile_source(out.read_text(encoding="utf-8"), "billing.rvl")


def test_cli_refuses_with_a_json_diagnostic(tmp_path, capsys):
    card = tmp_path / "card.json"
    card.write_text(json.dumps(_card(protocolVersion="0.3.0")), encoding="utf-8")
    assert main(["import", "a2a", str(card), "--json-diagnostics"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert "1.0.0" in report["diagnostics"][0]["message"]
    assert "#/protocolVersion" in report["diagnostics"][0]["message"]


def test_cli_allow_plaintext(tmp_path):
    out = tmp_path / "local.rvl"
    assert main(["import", "a2a", LOCAL, "--allow-plaintext",
                 "--backend", "py", "-o", str(out)]) == 0
    compile_source(out.read_text(encoding="utf-8"), "local.rvl")
    assert main(["import", "a2a", LOCAL, "-o", str(out)]) == 1


# ------------------------------- the generated crossing, executed (py backend)

def _run_py_body(reply: dict, *, backend_source: str | None = None):
    """Execute the generated python host body against a stubbed transport.

    The body is real code, not a stub, so the slice's load-bearing refusal — a
    non-terminal Task is a fault, never a poll — is testable directly rather
    than only greppable.
    """
    import io
    import textwrap
    import urllib.request

    source = backend_source or import_a2a_file(BILLING, backend="py")
    ir = compile_source(source, "billing.rvl")
    body = textwrap.indent(textwrap.dedent(ir["externs"][0]["bodies"]["py"]), "    ")

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    calls: list[bytes] = []

    class _Opener:
        """The body opens through `build_opener(...)`, not `urlopen`, because it
        installs a redirect-refusing handler (`revl.crossing_redirect`). The
        stub is therefore an OPENER, and the tests below stay about the JSON-RPC
        contract; the redirect policy itself is exercised against a real local
        server in `tests/test_crossing_redirect_policy.py`."""

        def open(self, request, *args, **kwargs):
            calls.append(request.data)
            return _Resp(json.dumps(reply).encode())

    namespace = {"__name__": "generated"}
    exec(compile(f"def _crossing(message):\n{body}\n", "<a2a-body>", "exec"),
         namespace)
    original = urllib.request.build_opener
    urllib.request.build_opener = lambda *handlers: _Opener()
    try:
        return namespace["_crossing"]("ping"), calls
    finally:
        urllib.request.build_opener = original


def test_a_completed_task_round_trips_its_text_artifact():
    reply = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "task", "id": "t-1", "status": {"state": "completed"},
        "artifacts": [{"parts": [{"kind": "text", "text": "INV-1 is paid"}]}]}}
    text, calls = _run_py_body(reply)
    assert text == "INV-1 is paid"
    sent = json.loads(calls[0])
    assert sent["method"] == "message/send"
    assert sent["jsonrpc"] == "2.0"
    assert sent["params"]["message"]["parts"] == [{"kind": "text", "text": "ping"}]
    assert sent["params"]["message"]["metadata"]["revl.skill"] == "invoice-lookup"


def test_a_direct_message_reply_round_trips():
    reply = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "message", "role": "agent", "messageId": "m-1",
        "parts": [{"kind": "text", "text": "pong"}]}}
    text, _ = _run_py_body(reply)
    assert text == "pong"


@pytest.mark.parametrize("state", ["working", "input-required", "auth-required",
                                   "submitted", "unknown"])
def test_a_non_terminal_task_raises_and_never_polls(state):
    """Item 439's open question, refused instead of guessed at: this binding
    crosses once. It does not poll `tasks/get`, does not resubscribe, and does
    not silently return an empty answer."""
    reply = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "task", "id": "t-1", "status": {"state": state}}}
    with pytest.raises(RuntimeError) as excinfo:
        _run_py_body(reply)
    assert "non-terminal" in str(excinfo.value)
    assert state in str(excinfo.value)


@pytest.mark.parametrize("state", ["failed", "canceled", "rejected"])
def test_a_terminal_but_unsuccessful_task_is_a_fault(state):
    """A remote failure is a FAULT, not a quietly-empty string. Turning it into
    provider WITHDRAWAL is item 424(c)'s `remote` row (C2), not this slice."""
    reply = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "task", "id": "t-1", "status": {"state": state}}}
    with pytest.raises(RuntimeError) as excinfo:
        _run_py_body(reply)
    assert state in str(excinfo.value)


def test_a_jsonrpc_error_is_a_fault():
    reply = {"jsonrpc": "2.0", "id": "1",
             "error": {"code": -32601, "message": "no such method"}}
    with pytest.raises(RuntimeError) as excinfo:
        _run_py_body(reply)
    assert "JSON-RPC error" in str(excinfo.value)


def test_a_reply_of_only_non_text_parts_is_a_fault():
    """This slice transcribes text in / text out. A file or data part coming
    back is not flattened into a string behind the caller's back."""
    reply = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "task", "id": "t-1", "status": {"state": "completed"},
        "artifacts": [{"parts": [{"kind": "file", "file": {"uri": "x"}}]}]}}
    with pytest.raises(RuntimeError) as excinfo:
        _run_py_body(reply)
    assert "non-text parts" in str(excinfo.value)


def test_the_crossing_sends_no_credential():
    reply = {"jsonrpc": "2.0", "id": "1", "result": {
        "kind": "message", "parts": [{"kind": "text", "text": "ok"}]}}
    source = import_a2a(_card(url="https://alice:s3cr3t@billing.internal:8443/a2a"),
                        filename="card.json", backend="py")
    _text, calls = _run_py_body(reply, backend_source=source)
    assert b"s3cr3t" not in calls[0]
    assert b"alice" not in calls[0]


# --------------------------------- 5. the crossing's colour (issue #251)

def test_the_ts_backend_declares_the_crossing_async():
    """`revl import a2a --backend ts` used to emit `await` inside a SYNCHRONOUS
    ts function — output no `tsc` accepts, on the importer's DEFAULT backend.

    JavaScript has no blocking fetch, so a synchronous body is not available on
    that tier: the only answers were to declare the operation `async` or to
    refuse the tier the way PR #250's `remote` row does. This importer declares
    it, because unlike a `remote` row it writes the service, the extern and the
    provider together and has no consumer-visible declaration it would have to
    recolour behind someone's back.

    All three declarations have to carry it or the file does not compile: a
    sync provide method that reaches an async extern is refused by the
    async-propagation rule (A1), and a sync service operation is refused for a
    provider that declares itself async.
    """
    source = import_a2a(_card(), filename="card.json", backend="ts")
    assert "  emission async fn invoice_lookup(" in source
    assert "async fn a2a_billing_agent_invoice_lookup(" in source
    assert "    async fn invoice_lookup(message) = " in source

    ir = compile_source(source, "billing.rvl")
    extern = next(e for e in ir["externs"]
                  if e["name"] == "a2a_billing_agent_invoice_lookup")
    assert extern["async"] is True, (
        "the IR must carry the colour, or the emitters have nothing to read "
        "and `await` lands in a plain `function` again")
    method = ir["services"]["BillingAgent"]["methods"]["invoice_lookup"]
    assert method["async"] is True, (
        "asynchrony crosses a component boundary only by DECLARATION "
        "(docs/design/async-extern.md §3) — a consumer reads it off the "
        "service, it is never smuggled in by the provider")


def test_the_py_backend_stays_sync():
    """The colour a generated file declares is the colour of the ONE host body
    it carries. The `@py` body is `urllib.request.urlopen`, which BLOCKS rather
    than suspends, and py is a coloured tier — so declaring it `async` would
    wrap a blocking call in an `async def`, stall the caller's loop, and colour
    every py caller for a suspension that never happens."""
    source = import_a2a(_card(), filename="card.json", backend="py")
    assert "  emission fn invoice_lookup(" in source
    assert "async" not in source.split("service BillingAgent")[1]

    ir = compile_source(source, "billing.rvl")
    extern = next(e for e in ir["externs"]
                  if e["name"] == "a2a_billing_agent_invoice_lookup")
    assert "async" not in extern


def test_the_ts_backend_fixture_is_current():
    """The a2a-to-ts typecheck fixture must be what this importer emits TODAY.

    Issue #251's finding was not that the gate was unsound, it was that no
    fixture reached this path — so the emitted output entered no typecheck.
    `backends/typescript/tests/fixtures/a2a_agent.ir.json` is that input, and
    it is a checked-in file: without this pin, an importer change would leave
    the gate typechecking output nobody emits any more, which is the same hole
    one step removed.

    Regenerate with:
        python3 backends/typescript/tests/fixtures/_gen_a2a_agent.py
    """
    fixtures = ROOT / "backends" / "typescript" / "tests" / "fixtures"
    sys.path.insert(0, str(fixtures))
    try:
        import _gen_a2a_agent  # noqa: PLC0415
    finally:
        sys.path.remove(str(fixtures))

    source, ir = _gen_a2a_agent.generate()
    stale = ("is stale. Regenerate it with `python3 backends/typescript/tests/"
             "fixtures/_gen_a2a_agent.py` and commit, or the ts typecheck gate "
             "(issue #219) is pointed at output this importer no longer emits.")
    assert _gen_a2a_agent.RVL.read_text(encoding="utf-8") == source, \
        f"{_gen_a2a_agent.RVL.name} {stale}"
    assert json.loads(_gen_a2a_agent.IR.read_text(encoding="utf-8")) == ir, \
        f"{_gen_a2a_agent.IR.name} {stale}"

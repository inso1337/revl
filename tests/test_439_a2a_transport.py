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

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.composition import compile_composition, resolve_file  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.import_a2a import A2A_VERSION  # noqa: E402

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
    the intended settlement, R2/R3), never a quietly-empty result."""
    text = _synth_source(tmp_path, WITHDRAW)
    assert 'raise RuntimeError("a2a: transport failure") from _exc' in text
    assert "return Err(" not in text


def test_on_failure_result_brings_it_back_in_band(tmp_path):
    """`on_failure(result)`, admitted because the method returns
    `Result[Str, Str]` (D-424c.3): a transport failure and a peer error both
    come back as `Err`, not as a raised fault."""
    base = WITHDRAW.replace("through a2a", "through a2a\n    on_failure(result)")
    text = _synth_source(tmp_path, base, services=AGENT_RESULT)
    assert 'return Err("a2a: transport failure")' in text
    assert 'return Ok(_text)' in text
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
    assert "exactly one `Str` message parameter" in message


def test_a_non_str_parameter_is_refused(tmp_path):
    """The message text is a `Str`. An `Int` parameter has no A2A text `Part`
    this slice projects."""
    services = """
service Agent {
  emission fn ask(count: Int) -> Str
}
"""
    write(tmp_path, services=services, base=WITHDRAW)
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    assert "is `Int`, not the `Str`" in str(excinfo.value)


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

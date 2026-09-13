"""The A2A 1.0.0 boundary gates, shared by every generated A2A crossing.

`src/revl/a2a_boundary.py` (item 439, issue #118). Three generated `@py` bodies
cross a peer's seam in this tree (`revl import a2a`'s single crossing, the
`remote ... through a2a[_rest]` row's single crossing, and the four-op Task
projection), and this module is the one place they share what they do with a
reply, so an external peer cannot be read differently depending on which entry
point generated the client.

The behaviour is exercised end to end, against executed bodies, in
`tests/test_439_a2a_transport.py` (both wires, the four ops) and
`tests/test_import_a2a.py` (the importer). What is pinned HERE is the contract
those three share: the constants that must not drift from the F5 funnel the
placement seam joins, and the fact that each gate is emitted by the shared
module rather than re-spelled per call site.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import a2a_boundary  # noqa: E402
from revl.a2a_task import task_body  # noqa: E402
from revl.import_a2a import (  # noqa: E402
    _py_a2a_body,
    _ts_body,
    _ts_body_rest,
)
from revl.synthesize import _py_body_a2a  # noqa: E402


def _fault(indent: int, expr: str) -> str:
    return " " * indent + f"raise RuntimeError({expr})\n"


# ------------------------------------------------ the F5 contract, not a copy

def test_the_placeholder_and_the_bound_match_the_seams_funnel():
    """The A2A funnel is item 421 F5 joined at this boundary, not a second
    redaction with its own vocabulary: an operator reading a scrubbed A2A fault
    beside a scrubbed seam fault must not have to learn two markers."""
    sys.path.insert(0, str(ROOT / "backends" / "python"))
    import confidential  # noqa: PLC0415

    assert a2a_boundary.REDACTED_ARG == confidential.REDACTED_ARG
    assert a2a_boundary.MIN_MATCHABLE_ARG == confidential._MIN_MATCHABLE_ARG


def test_the_funnel_is_emitted_as_source_not_imported():
    """Emitted, for the reason `revl.crossing_redirect` gives about the redirect
    policy: the rule is readable in the file an operator reviews rather than
    applied from a runtime import the generated file would have to trust."""
    body = a2a_boundary.py_funnel("_args")
    assert "def _scrub(_text):" in body
    assert "_arg_needles(_args, _needles)" in body
    assert "import" not in body


def test_every_generated_py_crossing_carries_the_same_gates():
    """One boundary, three emitters. A gate that existed on one wire and not
    another would be a peer read differently per entry point."""
    bodies = [
        _py_body_a2a("agent.example:8443", "ask", False, label="@agent"),
        _py_body_a2a("agent.example:8443", "ask", True, label="@agent"),
        _py_a2a_body("https://agent.example", "ask", follow_redirects=False,
                     rest=False, in_modality="text", out_modality="text"),
        task_body("start", "https://agent.example", "research", label="agent"),
        task_body("poll", "https://agent.example", "research", label="agent"),
        task_body("reply", "https://agent.example", "research", label="agent"),
        task_body("cancel", "https://agent.example", "research", label="agent"),
    ]
    for body in bodies:
        assert "_corr = str(_uuid.uuid4())" in body
        assert "if not isinstance(_rpc, dict):" in body
        assert 'if _rpc.get("jsonrpc") != "2.0":' in body
        assert 'if _rpc.get("id") != _corr:' in body
        assert "def _scrub(_text):" in body
        assert "_scrub(" in body.split("def _scrub(_text):", 1)[1]


def test_the_rest_wire_gets_the_shape_gates_and_no_envelope_check():
    """A2A 1.0.0's HTTP+JSON/REST reply is the bare `Task`/`Message` and echoes
    nothing a client could check, so the correlation rides ONE-WAY there. A
    check the wire cannot make is not emitted as if it could."""
    rest = _py_body_a2a("agent.example:8443", "ask", False, rest=True,
                        label="@agent")
    assert a2a_boundary.CORRELATION_KEY in rest       # one-way, on the wire
    assert 'if _rpc.get("id") != _corr:' not in rest  # nothing to check it with
    assert "if not isinstance(_result, dict) or not _result:" in rest


def test_the_correlation_refusal_renders_nothing_of_the_peers():
    """The gate that exists because peer text is a claim must not put
    peer-chosen text on the error channel: the id the peer DID answer with is
    never reported."""
    gates = a2a_boundary.py_envelope_gates(_fault)
    refusal = gates.split('if _rpc.get("id") != _corr:', 1)[1]
    assert "correlation id" in refusal
    assert '_rpc.get("id")' not in refusal
    assert "%" not in refusal


def test_the_identity_gate_is_emitted_only_where_a_task_is_already_held():
    """`_start` mints the task identity, so it has none to correlate; the other
    three name a task they hold, so their reply must describe THAT task."""
    marker = 'if _result.get("kind") == "task" and _result.get("id")'
    assert marker not in task_body("start", "https://x", "r", label="a")
    for kind in ("poll", "reply", "cancel"):
        assert marker in task_body(kind, "https://x", "r", label="a"), kind


def test_the_ts_crossing_correlates_its_reply():
    """The importer's coloured `@ts` body is the only A2A crossing on another
    tier, and it carries the same identity and the same three gates."""
    body = _ts_body("https://agent.example", "ask", follow_redirects=False)
    assert "const a2aCorr = crypto.randomUUID();" in body
    assert "id: a2aCorr," in body
    assert f'"{a2a_boundary.CORRELATION_KEY}": a2aCorr' in body
    assert 'if (rpc.id !== a2aCorr)' in body
    assert 'if (rpc.jsonrpc !== "2.0")' in body


# ------------------------------------------- the funnel, on BOTH tiers

def test_the_ts_placeholder_and_the_bound_match_the_seams_funnel():
    """Same claim as the py pin one file over: the ts funnel is item 421 F5
    joined at this boundary, not a second redaction with its own vocabulary.
    Read off `backends/typescript/bridge.ts` as TEXT, because that file is the
    node consumer's standalone module and imports nothing this test could."""
    bridge = (ROOT / "backends" / "typescript" / "bridge.ts").read_text(
        encoding="utf-8")
    assert f"export const REDACTED_ARG = '{a2a_boundary.REDACTED_ARG}'" in bridge
    assert (f"const MIN_MATCHABLE_ARG = {a2a_boundary.MIN_MATCHABLE_ARG}\n"
            in bridge)

    emitted = a2a_boundary.ts_funnel("[message]")
    assert f'const A2A_REDACTED_ARG = "{a2a_boundary.REDACTED_ARG}";' in emitted
    assert (f"const A2A_MIN_MATCHABLE_ARG = "
            f"{a2a_boundary.MIN_MATCHABLE_ARG};") in emitted


def test_every_generated_ts_crossing_carries_the_same_funnel():
    """Two ts bodies reach a peer (`message/send` and the REST `message:send`),
    and a funnel on one wire and not the other would be a boundary a peer could
    pick by declaring a `preferredTransport`.

    The four sites are the four peer-authored fields a reply renders into THIS
    composition's fault text. `a2a: transport failure (HTTP ...)` is not among
    them: a status code is the transport's, not the peer's prose.
    """
    sites = (
        "throw new Error(a2aScrub(`a2a: JSON-RPC error ${rpc.error.code}`));",
        "a2a: task returned non-terminal state '${state}'",
        "throw new Error(a2aScrub(`a2a: task ended '${state}'`));",
        "throw new Error(a2aScrub(`a2a: unexpected result kind '${kind}'`));",
    )
    jsonrpc = _ts_body("https://agent.example", "ask", follow_redirects=False)
    rest = _ts_body_rest("https://agent.example", "ask", follow_redirects=False)

    for body, name in ((jsonrpc, "message/send"), (rest, "message:send")):
        assert "function a2aScrub(hostText: string): string {" in body, name
        assert "a2aArgNeedles([message], needles);" in body, name
        for site in sites:
            if site.startswith("throw new Error(a2aScrub(`a2a: JSON-RPC"):
                # A REST reply has no envelope, so it has no `error` member to
                # unwrap: an A2A error arrives as a non-2xx status.
                assert (site in body) is (name == "message/send"), (name, site)
                continue
            assert site in body, (name, site)
        assert "a2aScrub(\n            `a2a: task returned" in body, name


def test_the_ts_funnel_is_emitted_as_source_not_imported():
    """Emitted, for the same reason the py funnel is: the rule is readable in
    the file an operator reviews rather than applied from a runtime import the
    generated file would have to trust. `bridge.ts` spells it a second time for
    exactly this reason and says so."""
    emitted = a2a_boundary.ts_funnel("[message]")
    assert "function a2aArgNeedles(value: unknown, into: Set<string>): void {" \
        in emitted
    assert "import" not in emitted
    assert "require(" not in emitted

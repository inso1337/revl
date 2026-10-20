"""Issue #2518: whether a swap's successor is agent-authored is decided by the
caller, through a required `agent_authored` argument to `Session.swap`.

Before the fix `Session.swap` worked the author out for itself by looking the
MCP server module up and comparing its `SESSION` with `self`. Any other Session
was treated as operator-authored, so a swap of an untrusted author's candidate
through a second Session handed a multi-realm frame's state off warm, the
leak issue #2510 closed for the server's own session. Now every caller says
which author it serves, and a caller that does not say is a TypeError.

The frame here (the #2510 shape) isolates `kv` in tenant_a and hands off a
shared `board`; the successor points `board` at the tenant_a store. A
successor the agent wrote starts cold; one the operator wrote migrates. A
files-only candidate outside every export area is the operator's own text only
while host code is untrusted (issue #2672); under `--author-trust trusted` it
is the agent's too, so the controls below pin both directions.

These drive a live composition, so they need the cordis-py runtime.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.mcp import repair as repair_mod  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402
from revl.mcp.session import Session  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a swap drives a live composition; install the runtime with "
           "`sh backends/python/setup.sh`")

SECRET = ("tenant_a_secret", "s3cr3t")
BINDING = (("tenant", "tenant_a"),)

REST = (
    "service Kv {\n  fn get(k: Str) -> Opt[Str]\n  fn set(k: Str, v: Str)\n"
    "  fn unset(k: Str)\n}\n\n")


def _app(realm: str) -> str:
    return (
        "component TenantAApp requires kv: Kv {\n"
        f"  isolate kv in {realm}\n"
        '  effect kv.set("who", "alice") undo kv.unset("who")\n'
        "}\n")


def _mixed(board_reads: str, realm: str) -> str:
    """One frame, two stores: `secret` serves `kv` in `realm`, `open` serves
    the shared `board`, and the hand-off is on `board`."""
    return (
        "component Mixed provides kv: Kv, board: Kv {\n"
        f"  isolate kv in {realm}\n"
        "  handoff board: Map[Str, Str]\n"
        "  let secret = effect Map.new() undo secret.drop()\n"
        "  let open = effect Map.new() undo open.drop()\n"
        "  provide kv {\n"
        "    fn get(k) = secret.get(k)\n"
        "    fn set(k, v) { effect secret.insert(k, v) undo secret.remove(k) }\n"
        "    fn unset(k) = secret.remove(k)\n"
        "  }\n"
        "  provide board {\n"
        f"    fn get(k) = {board_reads}.get(k)\n"
        "    fn set(k, v) { effect open.insert(k, v) undo open.remove(k) }\n"
        "    fn unset(k) = open.remove(k)\n"
        "  }\n"
        "}\n")


OPERATOR = REST + _app('realm("tenant_a")') + _mixed("open", 'realm("tenant_a")')
# what the untrusted author writes: the operator binds `?tenant` to tenant_a
AGENT_MIXED = _mixed("secret", "realm(?tenant)")
AGENT_WHOLE = REST + _app("realm(?tenant)") + AGENT_MIXED
# the same successor as the operator writes it
OPERATOR_MIXED = _mixed("secret", 'realm("tenant_a")')

POLICY = {"eligible": [{"component": "*"}], "mayTouch": ["*"],
          "ackOnWiden": False}


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def _fresh_server_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    server_mod.UNDO_STACK.clear()
    authoring = server_mod.AUTHORING
    yield
    server_mod.AUTHORING = authoring
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.SESSION.proposals = {}
    server_mod.SESSION.pending_draft = None
    server_mod.SESSION.draft = None
    server_mod.UNDO_STACK.clear()


def _untrusted() -> None:
    server_mod.set_authoring_trust(host_code=False, realm_bindings=BINDING)


def _operator_file(tmp_path: Path) -> None:
    """The operator's composition, loaded from a jailed file."""
    target = tmp_path / "running.rvl"
    target.write_text(OPERATOR, encoding="utf-8")
    loaded = _call("revl_load", {"files": [str(target)]})
    assert loaded["ok"] is True, loaded
    _secret_store(server_mod.SESSION).insert(*SECRET)


def _secret_store(session: Session):
    resources = session._frame_resources(session._driver.fibers.get("Mixed"))
    return resources[0]


def _board_get(session: Session) -> object:
    return session.call("board", "get", [SECRET[0]]).get("result")


def _second_session() -> Session:
    """A Session that is not the MCP server's, holding the operator's frame."""
    session = Session()
    session.load(compile_source(OPERATOR, "running.rvl"),
                 origin={"source": OPERATOR})
    _secret_store(session).insert(*SECRET)
    assert session is not server_mod.SESSION
    return session


def _successor(mixed: str) -> dict:
    return compile_source(REST + _app('realm("tenant_a")') + mixed,
                          "successor.rvl")


def _assert_cold(handoff: dict) -> None:
    assert handoff["board"]["migrated"] is False, handoff
    assert handoff["board"]["reason"] == "multi-realm", handoff


@pytest.fixture
def recorded(monkeypatch):
    """Every `agent_authored` value the server's session is swapped with."""
    seen = []
    original = server_mod.SESSION.swap

    def swap(*args, **kwargs):
        seen.append(kwargs.get("agent_authored", "<absent>"))
        return original(*args, **kwargs)

    monkeypatch.setattr(server_mod.SESSION, "swap", swap)
    return seen


@needs_runtime
def test_untrusted_swap_of_a_multi_realm_frame_starts_cold(tmp_path, recorded):
    _untrusted()
    _operator_file(tmp_path)
    swapped = _call("revl_swap", {"source": REST + AGENT_MIXED,
                                  "replacing": ["Mixed"]})
    assert swapped["ok"] is True, swapped
    assert recorded == [True]
    assert _board_get(server_mod.SESSION) != SECRET[1]
    _assert_cold(swapped["handoff"])


@needs_runtime
def test_second_session_with_agent_authored_starts_cold():
    """The fail-open case: the author is the argument, not whether this
    Session happens to be the server module's `SESSION`."""
    session = _second_session()
    try:
        state = session.swap(_successor(OPERATOR_MIXED),
                             origin={"source": OPERATOR_MIXED},
                             agent_authored=True)
        assert _board_get(session) != SECRET[1]
        _assert_cold(state["handoff"])
    finally:
        session.unload()


@needs_runtime
def test_repair_loop_on_a_second_session_starts_cold():
    """The repair loop swapping an untrusted author's candidate into a
    Session that is not the server's: still agent-authored."""
    _untrusted()
    session = _second_session()
    try:
        result = repair_mod.run_repair(session, {
            "component": "Mixed", "candidate": {"source": AGENT_WHOLE},
            "selfRepairPolicy": POLICY})
        assert result["incident"]["status"] == repair_mod.STATUS_REPAIRED, result
        assert _board_get(session) != SECRET[1]
        _assert_cold(result["remediation"]["swap"]["handoff"])
    finally:
        session.unload()


@needs_runtime
def test_untrusted_repair_of_a_multi_realm_frame_starts_cold(tmp_path, recorded):
    _untrusted()
    _operator_file(tmp_path)
    result = _call("revl_repair", {
        "component": "Mixed", "candidate": {"source": AGENT_WHOLE},
        "selfRepairPolicy": POLICY})
    assert result["incident"]["status"] == repair_mod.STATUS_REPAIRED, result
    assert recorded == [True]
    assert _board_get(server_mod.SESSION) != SECRET[1]
    _assert_cold(result["remediation"]["swap"]["handoff"])


@needs_runtime
def test_control_operator_file_swap_hands_off_warm(tmp_path):
    """The operator form that stays legal after the trusted-mode rule below:
    the successor is written to a jailed file outside every export area and
    host code is UNTRUSTED, so the compile reports no transport origin and the
    file is the operator's own text. The hand-off migrates warm. The trusted
    direction is the inverted control below; transport text stays agent text
    under either trust, which tests/test_mcp_swap_template_2423.py pins."""
    _untrusted()
    _operator_file(tmp_path)
    successor = tmp_path / "successor.rvl"
    successor.write_text(
        REST + _mixed("open", 'realm("tenant_a")').replace(
            "fn unset(k) = open.remove(k)", "fn unset(key) = open.remove(key)"),
        encoding="utf-8")
    swapped = _call("revl_swap", {"files": [str(successor)],
                                  "replacing": ["Mixed"]})
    assert swapped["ok"] is True, swapped
    assert _secret_store(server_mod.SESSION).data[SECRET[0]] == SECRET[1]
    assert swapped["handoff"]["board"]["migrated"] is True


@needs_runtime
def test_control_trusted_file_swap_is_agent_text_and_starts_cold(tmp_path):
    """The inverted control for the trusted-mode rule (issue #2672): the same
    jailed file outside every export area, now under
    `set_authoring_trust(host_code=True)`. The operator vouched for host
    bodies that can rewrite any file the agent can reach, so the predicate
    reports this files-only candidate authored and the warm hand-off is gone.

    This frame declares `handoff board` and has no live instance state, so the
    agent-authored direction starts cold under the #2510 multi-realm rule
    rather than raising STATE_UNDISCLOSED; the disclosure refusal for a
    files-only successor that changes live operator instance state is pinned
    in tests/test_mcp_swap_template_2423.py
    (`test_a_trusted_author_files_only_swap_of_a_rewritten_dependency_refuses`).
    Before the predicate change the candidate compiled unprofiled and read as
    the operator's own: the hand-off migrated warm and the seeded tenant_a
    secret rode onto the successor."""
    server_mod.set_authoring_trust(host_code=True)
    _operator_file(tmp_path)
    successor = tmp_path / "successor.rvl"
    successor.write_text(
        REST + _mixed("open", 'realm("tenant_a")').replace(
            "fn unset(k) = open.remove(k)", "fn unset(key) = open.remove(key)"),
        encoding="utf-8")
    swapped = _call("revl_swap", {"files": [str(successor)],
                                  "replacing": ["Mixed"]})
    assert swapped["ok"] is True, swapped
    assert swapped["swapped"] is True
    assert swapped["handoff"]["board"]["migrated"] is False
    assert swapped["handoff"]["board"]["reason"] == "multi-realm"
    assert SECRET[0] not in _secret_store(server_mod.SESSION).data
    assert _board_get(server_mod.SESSION) is None


@needs_runtime
def test_control_second_session_operator_swap_hands_off_warm():
    session = _second_session()
    try:
        state = session.swap(_successor(OPERATOR_MIXED),
                             origin={"source": OPERATOR_MIXED},
                             agent_authored=False)
        assert _board_get(session) == SECRET[1]
        assert state["handoff"]["board"]["migrated"] is True
    finally:
        session.unload()


def test_swap_without_the_author_argument_is_a_type_error():
    with pytest.raises(TypeError, match="agent_authored"):
        Session().swap({"components": []})


@needs_runtime
def test_cli_repair_by_the_operator_hands_off_warm(tmp_path, capsys):
    """`revl repair` on the command line: the operator wrote the composition
    and the candidate, so the swap is not agent-authored even while the
    server's trust global holds its untrusted default."""
    import json

    from revl.__main__ import main

    _untrusted()
    running = tmp_path / "running.rvl"
    running.write_text(OPERATOR, encoding="utf-8")
    candidate = tmp_path / "candidate.rvl"
    candidate.write_text(REST + _app('realm("tenant_a")') + OPERATOR_MIXED,
                         encoding="utf-8")
    policy = tmp_path / "policy.txt"
    policy.write_text("component * may self-repair\n"
                      "self-repair may touch *\nself-repair may widen\n",
                      encoding="utf-8")
    rc = main(["repair", str(running), "--component", "Mixed",
               "--candidate", str(candidate),
               "--self-repair-policy", str(policy), "--json"])
    dossier = json.loads(capsys.readouterr().out)
    assert rc == 0, dossier
    assert dossier["incident"]["status"] == repair_mod.STATUS_REPAIRED, dossier
    assert dossier["remediation"]["swap"]["handoff"]["board"]["migrated"] is True

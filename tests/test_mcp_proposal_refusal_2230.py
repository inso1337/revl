"""A refusal made while a proposal is held names the proposal (issue #2230).

A speculative change builds on the caller's held proposal, so a later change
that names a declaration the proposal removed is refused: the namespace is the
proposal, by design. What the refusal says is the point here. It must not
claim the declaration does not exist (the running composition has it, and the
same response's `sessionState` lists it); it must carry the `proposal` block
and `speculative`, as the success path does; and its note must say the held
proposal is still held and unchanged, not only that the running composition is.
"""

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the change verbs boot a composition and need the cordis-py runtime: "
           "install it with `sh backends/python/setup.sh`")

#: `TenantAApp` without its realm clause: TenantAApp is named by this module's
#: staged withdraw, so it no longer has an admitted placement to lose. TenantBApp
#: (below) survives, so it keeps its clause.
A_APP = ('component TenantAApp requires kv: Kv {\n'
         '  intercept kv with { quota: 5, tags: ["tenant_a"] }\n'
         '  effect kv.set("who", "alice") undo kv.unset("who")\n'
         '}\n')
#: TenantBApp likewise, but keeping the clause through the placeholder the
#: operator binds (issues #1728, #2585). TenantBApp survives this module's
#: staged withdraw, so dropping its clause would move a surviving requirer out
#: of `tenant_b` and is refused: the operator-bound placeholder names the same
#: realm without the author naming it.
B_APP = (A_APP.replace("TenantAApp", "TenantBApp").replace("alice", "bob")
         .replace("tenant_a", "tenant_b")
         .replace("requires kv: Kv {",
                  'requires kv: Kv {\n  isolate kv in realm(?tenant_b)'))
#: The clause above, on its own line.
CLAUSE_B = '  isolate kv in realm(?tenant_b)\n'
NOT_DECLARED = "no top-level declaration matches"
REPLACING = ["TenantAStore", "TenantAApp"]
#: The operator's bindings: each placeholder to the realm the example names.
BINDINGS = (("tenant_b", "tenant_b"),)


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _replace(component: str, source: str, **extra) -> dict:
    return _call("revl_change", {**extra, "replace": {"component": component,
                                                      "source": source}})


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    session = server_mod.SESSION
    server_mod.set_authoring_trust(roots=(str(tmp_path),),
                                   realm_bindings=BINDINGS)
    yield tmp_path
    if session.loaded:
        session.unload()
    session.proposals = {}
    session.pending_draft = None
    session.draft = None
    server_mod.AUTHORING = old


@pytest.fixture
def staged(clean_session):
    """The issue's reproduction up to the staged withdraw."""
    path = clean_session / "tenants.rvl"
    shutil.copy(ROOT / "examples" / "tenants.rvl", path)
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    staged = _call("revl_change", {"withdraw": {"component": "TenantAStore",
                                                "cascade": True}})
    assert staged["ok"] is True and staged["speculative"] is True
    assert staged["proposal"] == {"generation": 1, "replacing": REPLACING}
    return path


def _message(result: dict) -> str:
    assert result["ok"] is False
    return result["diagnostics"][0]["message"]


def test_a_name_the_proposal_removed_is_refused_as_removed(staged):
    result = _replace("TenantAApp", A_APP)
    message = _message(result)
    assert NOT_DECLARED not in message
    assert "`TenantAApp` was removed by your held proposal" in message
    assert "removed `TenantAStore`, `TenantAApp`" in message
    assert "running composition still has it" in message
    assert "revl_change {commit: true}" in message
    assert "revl_change {discard: true}" in message
    assert "name a declaration the proposal still has" in message
    assert [n["arguments"] for n in result["next"]] == [{"commit": True},
                                                        {"discard": True}]
    # the response that refuses it still lists it as running
    assert "TenantAApp" in result["sessionState"]["components"]


def test_the_refusal_carries_the_proposal_and_speculative(staged):
    result = _replace("TenantAApp", A_APP)
    assert result["ok"] is False
    assert result["speculative"] is True
    assert result["proposal"] == {"generation": 1, "replacing": REPLACING}


def test_the_note_says_the_proposal_is_still_held(staged):
    note = _replace("TenantAApp", A_APP)["note"]
    assert "the running composition is unchanged" in note
    assert "held proposal is still held and unchanged" in note


def test_a_committing_withdraw_of_a_removed_name_says_so(staged):
    result = _call("revl_change", {"withdraw": {"component": "TenantAStore",
                                                "cascade": True},
                                   "commit": True})
    message = _message(result)
    assert NOT_DECLARED not in message
    assert "`TenantAStore` was removed by your held proposal" in message
    assert result["speculative"] is False
    assert result["proposal"] == {"generation": 1, "replacing": REPLACING}


def test_the_refusal_leaves_the_proposal_to_commit(staged):
    _replace("TenantAApp", A_APP)
    committed = _call("revl_change", {"commit": True})
    assert committed["ok"] is True and committed["committed"] is True
    assert committed["sessionState"]["components"] == ["TenantBStore",
                                                       "TenantBApp"]


def test_a_survivor_still_resolves_in_the_proposal(staged):
    """TenantBApp is not named by the withdraw, so it is still resolvable in
    the proposal. It keeps its `tenant_b` placement through the operator-bound
    placeholder, since an untrusted author may not name a realm and may not
    strip the clause from a requirer that stays admitted (issue #2585)."""
    result = _replace("TenantBApp", B_APP)
    assert result["ok"] is True and result["speculative"] is True
    assert result["proposal"]["generation"] == 1


def test_a_survivor_that_loses_its_realm_is_refused_in_the_proposal(staged):
    """The same survivor with its clause stripped is refused (G9, issue
    #2585): it would leave the realm the operator's realm-scoped rules select
    it by. The refusal still carries the proposal, so this too is #2230's
    shape, not the shared-realm edit the old `B_APP` claimed."""
    result = _replace("TenantBApp", B_APP.replace(CLAUSE_B, ""))
    assert result["ok"] is False
    assert "cannot remove the realm placement of `TenantBApp`" in _message(result)
    assert result["speculative"] is True
    assert result["proposal"] == {"generation": 1, "replacing": REPLACING}


def test_a_name_absent_everywhere_keeps_its_message(staged):
    result = _replace("Nope", A_APP.replace("TenantAApp", "Nope"))
    message = _message(result)
    assert message.startswith(f"{NOT_DECLARED} `Nope`")
    assert "held proposal" not in message
    # the proposal is still held, so the refusal still carries it
    assert result["proposal"] == {"generation": 1, "replacing": REPLACING}
    assert "held proposal is still held and unchanged" in result["note"]


def test_with_no_proposal_a_refusal_is_as_before(clean_session):
    path = clean_session / "tenants.rvl"
    shutil.copy(ROOT / "examples" / "tenants.rvl", path)
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    result = _replace("Nope", A_APP.replace("TenantAApp", "Nope"))
    assert _message(result).startswith(f"{NOT_DECLARED} `Nope`")
    assert "proposal" not in result and "speculative" not in result
    assert result["note"] == ("the change was not committed; the running "
                              "composition is unchanged")


def test_any_other_edit_refusal_keeps_its_own_message(staged):
    """Only the miss of a removed name is rewritten: a clash in the same edit
    keeps the real refusal."""
    result = _call("revl_edit", {"commit": False, "edits": [
        {"append": "component TenantBApp {}"},
        {"symbol": "TenantAApp", "remove": True}]})
    message = _message(result)
    assert "TenantBApp is already declared" in message
    assert "held proposal" not in message
    assert result["proposal"] == {"generation": 1, "replacing": REPLACING}


def _make_stale():
    session = server_mod.SESSION
    (proposal,) = session.proposals.values()
    proposal["generation"] = session._generation - 1


def test_a_stale_proposal_is_offered_only_the_discard(staged):
    _make_stale()
    result = _replace("TenantAApp", A_APP)
    message = _message(result)
    assert "`TenantAApp` was removed by your held proposal" in message
    assert "is stale" in message
    assert "{commit: true}" not in message
    assert [n["arguments"] for n in result["next"]] == [{"discard": True}]
    assert result["proposal"]["stale"] is True
    assert "{commit: true}" not in result["note"]
    assert "stale, so it cannot commit" in result["note"]

"""Undo, rollback and step back may not move a provider out of its realm (issue #2525).

An untrusted author may not delete an admitted `isolate <key> in realm("...")`
clause through an edit (issue #2487). Returning to an earlier generation
reaches the same composition by another door: after an operator places a
provider in a realm, the generation before that placement is still retained,
and replaying it moves the provision back into the shared realm. `revl_undo`
(and `revl_step_back`, which replays the undo a swap's answer carried) and
`revl_rollback` are refused for the untrusted author, with the running
composition left as it was.

Controls: a trusted operator may undo and roll back the placement, and an
untrusted undo or rollback that removes no placement is admitted.
"""

import importlib.util
import json
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

VAULT = '''service Kv {
  fn get(k: Str) -> Opt[Str]
  fn set(k: Str, v: Str)
  fn unset(k: Str)
}

component Vault provides kv: Kv {
  isolate kv in realm("tenant_a")
  let store = effect Map.new() undo store.drop()
  provide kv {
    fn get(k) = store.get(k)
    fn set(k, v) {
      effect store.insert(k, v)
      undo   store.remove(k)
    }
    fn unset(k) = store.remove(k)
  }
}
'''
CLAUSE = '  isolate kv in realm("tenant_a")\n'
SHARED = VAULT.replace(CLAUSE, "")
EXTRA = ('\ncomponent Ledger {\n'
         '  let log = effect Map.new() undo log.drop()\n'
         '}\n')
REFUSAL = "cannot remove the realm placement"


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _messages(result: dict) -> str:
    return " ".join(str(d.get("message", "")) + " " + str(d.get("hint", ""))
                    for d in result.get("diagnostics") or [])


def _realms(component: str) -> dict:
    for comp in server_mod.SESSION.ir["components"]:
        if comp["name"] == component:
            return dict(comp.get("isolate") or {})
    raise AssertionError(f"{component} is not running")


def _generation() -> int:
    return server_mod.SESSION._history[-1]["generation"]


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    session = server_mod.SESSION
    server_mod.UNDO_STACK.clear()
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    yield tmp_path
    if session.loaded:
        session.unload()
    session.proposals = {}
    session.pending_draft = None
    session.draft = None
    server_mod.UNDO_STACK.clear()
    server_mod.AUTHORING = old


def _run(tmp_path, first: str, second: str) -> None:
    """Load `first` from a file, then the operator swaps the file to `second`."""
    path = tmp_path / "vault.rvl"
    path.write_text(first)
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    path.write_text(second)
    swapped = _call("revl_swap", {"files": [str(path)]})
    assert swapped["ok"] is True, swapped


@pytest.fixture
def placed(clean_session):
    """Generation 1 runs Vault in the shared realm; the operator's swap then
    places it in realm("tenant_a"), which is what runs now."""
    _run(clean_session, SHARED, VAULT)
    assert _realms("Vault") == {"kv": "tenant_a"}
    return _generation()


def _assert_untouched(generation: int) -> None:
    assert _realms("Vault") == {"kv": "tenant_a"}
    assert _generation() == generation


def test_untrusted_undo_of_a_placement_is_refused(placed):
    assert server_mod.AUTHORING.profile() is not None  # the untrusted author
    result = _call("revl_undo", {})
    assert result["ok"] is False and result.get("undone") is False, result
    assert result.get("refused") is True, result
    assert REFUSAL in _messages(result) and "Vault" in _messages(result), result
    assert any(d.get("code") == "G9" for d in result["diagnostics"]), result
    _assert_untouched(placed)


def test_untrusted_rollback_of_a_placement_is_refused(placed):
    result = _call("revl_rollback", {})
    assert result["ok"] is False and not result.get("rolledBack"), result
    assert REFUSAL in _messages(result) and "Vault" in _messages(result), result
    assert "stateDropped" not in result, result  # refused before any teardown
    _assert_untouched(placed)
    # the target is kept, so an operator can still roll back
    assert server_mod.SESSION.previous is not None


def test_untrusted_step_back_of_a_placement_is_refused(placed):
    # the operator swap's answer carried `revl_undo` as its exact undo
    assert server_mod.UNDO_STACK.last()["undo"]["tool"] == "revl_undo"
    result = _call("revl_step_back", {})
    assert result["ok"] is False and not result.get("steppedBack"), result
    assert REFUSAL in _messages(result["result"]), result
    _assert_untouched(placed)


def test_trusted_operator_may_undo_and_roll_back_the_placement(placed):
    server_mod.set_authoring_trust(host_code=True)
    assert server_mod.AUTHORING.profile() is None
    undone = _call("revl_undo", {})
    assert undone["ok"] is True and undone.get("undone") is True, undone
    assert _realms("Vault") == {}
    # back to the placement, then a rollback moves it out again
    redone = _call("revl_undo", {"to": placed})
    assert redone.get("undone") is True, redone
    assert _realms("Vault") == {"kv": "tenant_a"}
    rolled = _call("revl_rollback", {})
    assert rolled["ok"] is True and rolled.get("rolledBack") is True, rolled
    assert _realms("Vault") == {}


def test_untrusted_undo_that_keeps_every_placement_is_admitted(clean_session):
    _run(clean_session, VAULT, VAULT + EXTRA)
    names = {c["name"] for c in server_mod.SESSION.ir["components"]}
    assert names == {"Vault", "Ledger"}
    result = _call("revl_undo", {})
    assert result["ok"] is True and result.get("undone") is True, result
    assert _realms("Vault") == {"kv": "tenant_a"}
    names = {c["name"] for c in server_mod.SESSION.ir["components"]}
    assert names == {"Vault"}


def test_untrusted_rollback_that_keeps_every_placement_is_admitted(clean_session):
    _run(clean_session, VAULT, VAULT + EXTRA)
    result = _call("revl_rollback", {})
    assert result["ok"] is True and result.get("rolledBack") is True, result
    assert _realms("Vault") == {"kv": "tenant_a"}


def test_operator_cli_undo_of_a_placement_is_admitted(placed, tmp_path,
                                                       capsys):
    """`revl undo history.json` is the operator's command line: it replays the
    history into its own session, with no authoring profile, so the realm
    check (an authoring rule) must not refuse it, whatever the MCP server's
    author trust is set to."""
    from revl.__main__ import main

    assert server_mod.AUTHORING.profile() is not None  # the MCP author is untrusted
    doc = server_mod.SESSION.history_document()
    path = tmp_path / "history.json"
    path.write_text(json.dumps(doc))
    rc = main(["undo", str(path)])
    captured = capsys.readouterr()
    assert REFUSAL not in captured.out + captured.err, captured
    assert rc == 0, captured
    assert "undone: generation" in captured.out, captured

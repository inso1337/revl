"""`revl_change`: one intent-shaped call for the change loop (issue #1695).

In an agent benchmark one change took eight calls, five of them `revl_check`
polls. These tests hold the issue's exits and the three intents it starts
with:

* a withdraw-and-verify change completes in one call with the cascade reported;
* a failing verification commits nothing and reports why;
* the result names every component the change touched;

plus `{replace}`, `{edit}`, the optional gauntlet, a load from files in the same
call, and a draft whose holes the change fills and boots.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the change verbs boot a composition and need the cordis-py runtime "
           "— install it with `sh backends/python/setup.sh`")

SOURCE = ("service Store { fn get() -> Str }\n"
          "service Api { fn read() -> Str }\n"
          "service Clock { fn now() -> Int }\n"
          "\n"
          "// the backing store\n"
          "component MemStore provides store: Store {\n"
          '  provide store { fn get() = "stored" }\n'
          "}\n"
          "\n"
          "component ApiImpl requires store: Store provides api: Api {\n"
          "  provide api { fn read() = store.get() }\n"
          "}\n"
          "\n"
          "component FixedClock provides clock: Clock {\n"
          "  provide clock { fn now() = 7 }\n"
          "}\n")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _components() -> set[str]:
    return {c["name"] for c in server_mod.SESSION.ir["manifest"]["components"]}


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    yield tmp_path
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.SESSION.pending_draft = None
    server_mod.SESSION.draft = None
    server_mod.AUTHORING = old


@pytest.fixture
def loaded():
    assert _call("revl_load", {"source": SOURCE})["ok"] is True
    assert _components() == {"MemStore", "ApiImpl", "FixedClock"}


# ------------------------------------------------ withdraw


def test_a_withdraw_and_verify_change_completes_in_one_call(loaded):
    result = _call("revl_change", {"withdraw": "FixedClock"})
    assert result["ok"] is True and result["committed"] is True, result
    assert result["intent"] == "withdraw"
    assert result["verified"]["admission"] == "passed"
    assert result["plan"]["cascade"] == []
    assert result["components"] == [{"component": "FixedClock", "change": "removed"}]
    assert _components() == {"MemStore", "ApiImpl"}


def test_a_withdrawal_with_dependents_commits_nothing_and_says_why(loaded):
    result = _call("revl_change", {"withdraw": "MemStore"})
    assert result["ok"] is False and result["committed"] is False
    assert result["verified"]["admission"] == "refused"
    assert [c["component"] for c in result["plan"]["cascade"]] == ["ApiImpl"]
    message = result["diagnostics"][0]["message"]
    assert "withdrawing MemStore would leave 1 component(s) without a provider" \
        in message and "ApiImpl" in message
    assert "unchanged" in result["note"]
    # every component the change touched or would have, by name
    assert {"component": "ApiImpl", "change": "would lose a provider"} \
        in result["components"]
    assert _components() == {"MemStore", "ApiImpl", "FixedClock"}
    assert _call("revl_call", {"key": "api", "method": "read"})["result"] == "stored"


def test_a_cascading_withdrawal_takes_the_dependents_with_it(loaded):
    result = _call("revl_change", {"withdraw": {"component": "MemStore",
                                                "cascade": True}})
    assert result["committed"] is True, result
    assert {(c["component"], c["change"]) for c in result["components"]} == {
        ("MemStore", "removed"), ("ApiImpl", "removed")}
    assert _components() == {"FixedClock"}
    # the held source lost both declarations and MemStore's comment with it
    held = _call("revl_source", {"symbol": "FixedClock"})
    assert held["ok"] is True
    assert server_mod.SESSION.origin["source"].count("component") == 1
    assert "backing store" not in server_mod.SESSION.origin["source"]


# ------------------------------------------------ replace and edit


def test_replace_swaps_one_component_by_name(loaded):
    result = _call("revl_change", {"replace": {
        "component": "FixedClock",
        "source": "component FixedClock provides clock: Clock {\n"
                  "  provide clock { fn now() = 9 }\n}\n"}})
    assert result["committed"] is True, result
    assert result["components"] == [{"component": "FixedClock", "change": "changed"}]
    assert _call("revl_call", {"key": "clock", "method": "now"})["result"] == 9


def test_a_replace_that_breaks_a_guarantee_commits_nothing(loaded):
    result = _call("revl_change", {"replace": {
        "component": "FixedClock",
        "source": "component FixedClock provides clock: Clock {\n"
                  '  provide clock { fn now() = "nine" }\n}\n'}})
    assert result["committed"] is False
    assert result["diagnostics"][0]["code"] == "T1"
    assert _call("revl_call", {"key": "clock", "method": "now"})["result"] == 7


def test_edit_runs_revl_edits_patch(loaded):
    result = _call("revl_change", {"edit": {"edits": [
        {"anchor": '"stored"', "replacement": '"edited"'}]}})
    assert result["committed"] is True, result
    assert result["components"] == [{"component": "MemStore", "change": "changed"}]
    assert _call("revl_call", {"key": "api", "method": "read"})["result"] == "edited"


# ------------------------------------------------ verification


def test_the_gauntlet_passes_a_sound_change(loaded):
    result = _call("revl_change", {"withdraw": "FixedClock", "gauntlet": True})
    assert result["committed"] is True, result
    assert result["verified"]["gauntlet"] == "passed"


def test_a_failing_gauntlet_commits_nothing_and_reports_why(loaded, monkeypatch):
    from revl.mcp import gauntlet

    monkeypatch.setattr(gauntlet, "run", lambda session, arguments: {
        "ok": True, "verdict": "rejected", "tested": {}})
    result = _call("revl_change", {"withdraw": "FixedClock", "gauntlet": True})
    assert result["committed"] is False
    assert result["verified"]["gauntlet"] == "failed"
    assert "gauntlet did not pass" in result["diagnostics"][0]["message"]
    assert _components() == {"MemStore", "ApiImpl", "FixedClock"}


# ------------------------------------------------ load and drafts


def test_one_call_loads_files_and_changes_them(clean_session):
    path = clean_session / "app.rvl"
    path.write_text(SOURCE, encoding="utf-8")
    result = _call("revl_change", {"files": [str(path)], "withdraw": "FixedClock"})
    assert result["committed"] is True and result["loaded"] is True, result
    assert _components() == {"MemStore", "ApiImpl"}
    assert path.read_text(encoding="utf-8") == SOURCE


def test_filling_a_drafts_holes_and_booting_it_is_a_change(clean_session):
    scaffold = _call("revl_scaffold", {"service": "Greeter", "effect": False,
                                       "methods": ["greet() -> Str"]})
    opened = _call("revl_load", {"source": scaffold["source"]})
    assert opened["draft"] is True and not server_mod.SESSION.loaded
    result = _call("revl_change", {"edit": {"edits": [
        {"hole": opened["holes"][0]["line"], "expr": '"hi"'}]}})
    assert result["committed"] is True and result["booted"] is True, result
    assert _call("revl_call", {"key": "greeter", "method": "greet"})["result"] == "hi"


def test_one_intent_exactly():
    both = _call("revl_change", {"withdraw": "X", "edit": {"edits": []}})
    assert both["ok"] is False and both["committed"] is False
    assert "exactly one intent" in both["diagnostics"][0]["message"]
    none = _call("revl_change", {})
    assert none["ok"] is False

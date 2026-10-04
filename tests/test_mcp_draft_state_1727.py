"""A draft with open holes can be loaded into a drafting state (issue #1727).

`revl_scaffold` returns a draft with typed holes and the documented loop is
scaffold -> load -> fill -> boot, but `revl_load` refused any hole ("cannot
load: N open typed hole(s)") and `revl_edit` needed something loaded, so the
loop could not start. A scaffold-first agent arm made zero edits because of it.

These tests hold the issue's exits: the whole loop end to end with no disk
write; a draft never boots while a hole remains; a hole-free draft a gate
refuses reports why and stays a draft, and boots once the gate allows it.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402
from revl.policy import parse_policy  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="booting a draft needs the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = server_mod.SESSION
    sandbox = getattr(session, "sandbox", None)
    yield tmp_path
    if session.loaded:
        session.unload()
    session.pending_draft = None
    session.draft = None
    session.sandbox = sandbox


def _scaffold() -> dict:
    scaffold = _call("revl_scaffold", {"service": "Greeter", "effect": False,
                                       "methods": ["greet() -> Str",
                                                   "count() -> Int"]})
    assert scaffold["ok"] is True and scaffold["holeCount"] == 2, scaffold
    return scaffold


def _greet() -> str:
    return _call("revl_call", {"key": "greeter", "method": "greet"})["result"]


def test_scaffold_load_fill_boot_end_to_end_with_no_disk_write(clean_session):
    before = sorted(os.listdir(clean_session))
    loaded = _call("revl_load", {"source": _scaffold()["source"]})
    assert loaded["ok"] is True and loaded["draft"] is True, loaded
    assert loaded["booted"] is False and loaded["holeCount"] == 2
    assert not server_mod.SESSION.loaded
    first, second = sorted(loaded["holes"], key=lambda h: h["line"])
    assert first["fillSpec"]["expected"] == "Str"

    # one hole filled: the draft advances, still nothing boots
    step = _call("revl_edit", {"edits": [{"hole": first["line"], "expr": '"hello"'}]})
    assert step["ok"] is True and step["draft"] is True, step
    assert step["holeCount"] == 1 and step["booted"] is False
    assert not server_mod.SESSION.loaded
    assert _call("revl_state", {})["draft"] == {"holes": 1}

    # the last hole: the same call boots it through the load gates
    done = _call("revl_edit", {"edits": [{"hole": second["line"], "expr": "2"}]})
    assert done["ok"] is True, done
    assert done["booted"] is True and done["draft"] is False
    assert server_mod.SESSION.loaded
    assert _greet() == "hello"
    assert _call("revl_call", {"key": "greeter", "method": "count"})["result"] == 2
    assert "draft" not in _call("revl_state", {})
    assert sorted(os.listdir(clean_session)) == before


def test_a_draft_never_boots_while_a_hole_remains(clean_session):
    _call("revl_load", {"source": _scaffold()["source"]})
    retried = _call("revl_load", {})
    assert retried["ok"] is False and retried["draft"] is True
    assert retried["holeCount"] == 2 and not server_mod.SESSION.loaded
    # an edit that leaves a hole does not boot either
    holes = _call("revl_source", {"symbol": "GreeterProvider"})
    assert holes["ok"] is True and "hole[" in holes["text"]
    edit = _call("revl_edit", {"edits": [{"anchor": "fn greet() = hole[Str]",
                                          "replacement": "fn greet() = hole[Str]"}]})
    assert edit["draft"] is True and not server_mod.SESSION.loaded


def test_a_hole_free_draft_a_gate_refuses_stays_a_draft(clean_session):
    """Under an enforcing lease policy another operator holds the component's
    name: the cold load is fenced, the reason comes back, the draft is held
    hole-free, and it boots with `revl_load {}` once the lease is released."""
    session = server_mod.SESSION
    session.sandbox = parse_policy("leases enforced")
    session.leases.claim("GreeterProvider", "bob", ttl=600)
    loaded = _call("revl_load", {"source": _scaffold()["source"]})
    lines = sorted(h["line"] for h in loaded["holes"])
    refused = _call("revl_edit", {"edits": [{"hole": lines[0], "expr": '"hi"'},
                                            {"hole": lines[1], "expr": "1"}]})
    assert refused["ok"] is False and refused["draft"] is True, refused
    assert refused["booted"] is False and refused["lease"]["heldBy"] == "bob"
    assert not session.loaded
    session.leases.release("GreeterProvider", "bob")
    booted = _call("revl_load", {})
    assert booted["ok"] is True and booted["booted"] is True, booted
    assert _greet() == "hi"


def test_a_patch_that_does_not_compile_leaves_the_draft_unchanged(clean_session):
    loaded = _call("revl_load", {"source": _scaffold()["source"]})
    line = sorted(h["line"] for h in loaded["holes"])[0]
    bad = _call("revl_edit", {"edits": [{"hole": line, "expr": "42"}]})
    assert bad["ok"] is False and bad["draft"] is True
    assert bad["diagnostics"][0]["code"] == "T1"
    good = _call("revl_edit", {"edits": [{"hole": line, "expr": '"fine"'}]})
    assert good["holeCount"] == 1, good


def test_revl_edit_self_load_of_a_scaffold_opens_the_draft_and_edits_it(clean_session):
    source = _scaffold()["source"]
    result = _call("revl_edit", {"source": source, "edits": [
        {"anchor": 'hole[Str] "produce greet\'s Str result"', "replacement": '"one"'}]})
    assert result["ok"] is True and result["draft"] is True, result
    assert result["holeCount"] == 1 and not server_mod.SESSION.loaded


def test_unload_discards_a_held_draft(clean_session):
    _call("revl_load", {"source": _scaffold()["source"]})
    gone = _call("revl_unload", {})
    assert gone["ok"] is True and gone["discardedDraft"] is True
    assert server_mod.SESSION.pending_draft is None

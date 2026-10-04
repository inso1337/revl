"""Speculation by default, one explicit commit; the held source is the truth
and disk is an export (issue #1696, docs/design/1696-speculation.md).

The issue's exits:

* an agent can try and revert a change with no disk write;
* export writes exactly the held source;
* a commit is the only state change visible to a second session.

A "second session" is a second caller of the one server: another operator
identity, as two HTTP callers are. Proposals are per caller, so the second one
sees only what is committed.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.operator import Grant, Operator  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the change verbs boot a composition and need the cordis-py runtime "
           "— install it with `sh backends/python/setup.sh`")

SERVICE = "service Clock { fn now() -> Int }\n"
COMPONENT = ("component FixedClock provides clock: Clock {\n"
             "  provide clock {\n"
             "    fn now() = 7\n"
             "  }\n"
             "}\n")
SOURCE = SERVICE + COMPONENT


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _now() -> int:
    return _call("revl_call", {"key": "clock", "method": "now"})["result"]


def _set_now(value: int, **extra) -> dict:
    return _call("revl_change", {**extra, "replace": {
        "component": "FixedClock.clock.now", "source": f"fn now() = {value}"}})


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    session = server_mod.SESSION
    operator = getattr(session, "operator", None)
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    yield tmp_path
    session.operator = operator
    if session.loaded:
        session.unload()
    session.proposals = {}
    session.pending_draft = None
    session.draft = None
    server_mod.AUTHORING = old


@pytest.fixture
def files_loaded(clean_session):
    path = clean_session / "clock.rvl"
    path.write_text(SOURCE, encoding="utf-8")
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    return path


def _operator(token: str) -> Operator:
    """An operator that may edit, swap, load, call and read the composition."""
    return Operator(token, grants=(Grant(verbs=("edit", "swap", "load", "snapshot", "call"),
                                         subjects=("*",), allow=True),))


def _listing(directory: Path) -> dict:
    return {p.name: p.read_bytes() for p in directory.iterdir()}


# ------------------------------------------------ try and revert, no disk


def test_a_change_is_speculative_by_default_and_can_be_reverted(files_loaded):
    before = _listing(files_loaded.parent)
    tried = _set_now(9)
    assert tried["ok"] is True and tried["committed"] is False, tried
    assert tried["speculative"] is True
    assert tried["verified"]["admission"] == "passed"
    assert _now() == 7                       # the running composition is untouched
    proposed = _call("revl_source", {"symbol": "FixedClock.now", "proposal": True})
    assert "fn now() = 9" in proposed["text"]
    held = _call("revl_source", {"symbol": "FixedClock.now"})
    assert "fn now() = 7" in held["text"]    # the held source is unchanged too
    dropped = _call("revl_change", {"discard": True})
    assert dropped["discarded"] is True
    assert _now() == 7
    assert _listing(files_loaded.parent) == before


def test_speculative_changes_build_up_and_one_commit_lands_them(files_loaded):
    _set_now(8)
    second = _call("revl_change", {"edit": {"edits": [
        {"anchor": "fn now() = 8", "replacement": "fn now() = 11"}]}})
    assert second["committed"] is False and _now() == 7, second
    committed = _call("revl_change", {"commit": True})
    assert committed["committed"] is True, committed
    assert _now() == 11
    assert [t["symbol"] for t in committed["touched"]] == ["FixedClock.clock.now"]
    # nothing is held any more
    assert _call("revl_change", {"commit": True})["ok"] is False


def test_a_proposal_that_breaks_a_guarantee_is_refused_and_not_held(files_loaded):
    refused = _call("revl_change", {"replace": {
        "component": "FixedClock.clock.now", "source": 'fn now() = "seven"'}})
    assert refused["ok"] is False and refused["committed"] is False
    assert refused["diagnostics"][0]["code"] == "T1"
    assert server_mod.SESSION.proposals == {}


def test_revl_edit_can_propose_too(files_loaded):
    proposed = _call("revl_edit", {"commit": False, "edits": [
        {"anchor": "fn now() = 7", "replacement": "fn now() = 3"}]})
    assert proposed["ok"] is True and proposed["swapped"] is False, proposed
    # a proposal is where preflight matters most, so it carries the cascade too
    assert proposed["blastRadius"]["touched"] == ["FixedClock"], proposed
    assert _now() == 7
    assert _call("revl_change", {"commit": True})["committed"] is True
    assert _now() == 3


# ------------------------------------------------ the second session


def test_a_commit_is_the_only_change_a_second_caller_sees(files_loaded):
    session = server_mod.SESSION
    alice, bob = _operator("alice"), _operator("bob")
    session.operator = alice
    assert _set_now(5)["committed"] is False
    session.operator = bob
    assert _now() == 7
    assert "fn now() = 7" in _call("revl_source", {"symbol": "FixedClock.now"})["text"]
    assert _call("revl_change", {"commit": True})["ok"] is False  # bob has nothing
    session.operator = alice
    assert _call("revl_change", {"commit": True})["committed"] is True
    session.operator = bob
    assert _now() == 5


def test_a_stale_proposal_is_refused_rather_than_undoing_a_commit(files_loaded):
    session = server_mod.SESSION
    alice, bob = _operator("alice"), _operator("bob")
    session.operator = alice
    _set_now(5)
    session.operator = bob
    assert _set_now(6, commit=True)["committed"] is True
    session.operator = alice
    stale = _call("revl_change", {"commit": True})
    assert stale["ok"] is False and stale["committed"] is False
    assert "moved since this proposal" in stale["diagnostics"][0]["message"]
    assert _now() == 6


# ------------------------------------------------ export


def test_export_writes_exactly_the_held_source(files_loaded):
    assert _set_now(12, commit=True)["committed"] is True
    assert files_loaded.read_text(encoding="utf-8") == SOURCE   # not yet
    exported = _call("revl_export", {})
    assert exported["ok"] is True and exported["written"] == [str(files_loaded)]
    snapshot = _call("revl_snapshot", {})["snapshot"]["sources"]["files_content"]
    assert files_loaded.read_text(encoding="utf-8") == snapshot[str(files_loaded)]
    assert "fn now() = 12" in files_loaded.read_text(encoding="utf-8")
    assert _call("revl_export", {})["written"] == []     # nothing left to write


def test_export_never_writes_a_proposal(files_loaded):
    _set_now(13)
    assert _call("revl_export", {})["written"] == []
    assert files_loaded.read_text(encoding="utf-8") == SOURCE


def test_inline_export_needs_a_path_inside_the_roots(clean_session, tmp_path_factory):
    _call("revl_load", {"source": SOURCE})
    assert _call("revl_export", {})["ok"] is False
    outside = tmp_path_factory.mktemp("elsewhere") / "x.rvl"
    refused = _call("revl_export", {"path": str(outside)})
    assert refused["ok"] is False and not outside.exists()
    target = clean_session / "out.rvl"
    assert _call("revl_export", {"path": str(target)})["written"] == [str(target)]
    assert target.read_text(encoding="utf-8") == SOURCE
    again = _call("revl_export", {"path": str(target)})
    assert again["ok"] is False and "already exists" in again["diagnostics"][0]["message"]
    assert _call("revl_export", {"path": str(target), "overwrite": True})["ok"] is True
    assert not any(name.endswith(".revl-export") for name in os.listdir(clean_session))


# ------------------------------------------------ drafts


def test_a_speculative_fill_does_not_boot_a_draft_until_commit(clean_session):
    scaffold = _call("revl_scaffold", {"service": "Greeter", "effect": False,
                                       "methods": ["greet() -> Str"]})
    opened = _call("revl_load", {"source": scaffold["source"]})
    filled = _call("revl_change", {"edit": {"edits": [
        {"hole": opened["holes"][0]["line"], "expr": '"hi"'}]}})
    assert filled["ok"] is True and filled["committed"] is False, filled
    assert not server_mod.SESSION.loaded
    booted = _call("revl_change", {"commit": True})
    assert booted["committed"] is True, booted
    assert _call("revl_call", {"key": "greeter", "method": "greet"})["result"] == "hi"


# ------------------------------------------------ the undo each verb carries (#1703)


def test_a_committed_change_undoes_to_the_generation_before_it(files_loaded):
    committed = _set_now(9, commit=True)
    assert committed["committed"] is True, committed
    assert _now() == 9
    assert committed["undo"]["tool"] == "revl_undo", committed
    assert _call(committed["undo"]["tool"], committed["undo"]["arguments"])["ok"] is True
    assert _now() == 7


def test_an_export_says_it_has_no_inverse(files_loaded):
    exported = _call("revl_export", {})
    assert exported["ok"] is True, exported
    assert exported["undo"] is None and "disk" in exported["undoReason"]

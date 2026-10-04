"""`revl_counterfactual`: what the gate would have decided had the agent acted
differently (issue #1752).

A session driven through `revl_act` keeps its actions and the approvals minted
between them. A counterfactual replaces, inserts or drops one action and decides
both arms with the gate's pure parts, then reports where they diverge. Nothing
runs: the tests below check that no host body fires and the session's manifest
is unchanged.

The recording every test starts from, over the item-246 fixture:

    0  stash(target)        class (a)  executed
    1  enqueue(queue, m)    class (b)  deferred
    2  shout(sink, hi)      class (c)  ticket
       -- the ticket is approved --
    3  shout(sink, hi)      class (c)  executed, spends the approval
"""

import copy
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl import compile_source  # noqa: E402
from revl.mcp import server  # noqa: E402
from test_tool_loop_act_1708 import SOURCE  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the recording is made by a live cordis-py session; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)


def _session(tmp_path):
    from revl.mcp.session import Session

    session = Session()
    session._wal_path = str(tmp_path / "session.wal")
    session.approval_policy = "auto"
    session.load(copy.deepcopy(compile_source(SOURCE, "cf.rvl")), record=True)
    return session


@pytest.fixture
def recorded(monkeypatch, tmp_path):
    from revl.mcp.approval import ApprovalRequired

    target = tmp_path / "artifact.txt"
    target.write_text("deliverable", encoding="utf-8")
    paths = {"target": str(target), "queue": str(tmp_path / "queue.log"),
             "sink": str(tmp_path / "sink.log")}
    session = _session(tmp_path)
    session.act("ops", "stash", [paths["target"]])
    session.act("ops", "enqueue", [paths["queue"], "m"])
    with pytest.raises(ApprovalRequired) as exc:
        session.act("ops", "shout", [paths["sink"], "hi"])
    session.approve_ticket(exc.value.ticket["hash"])
    session.act("ops", "shout", [paths["sink"], "hi"])
    monkeypatch.setattr(server, "SESSION", session)
    yield session, paths, exc.value.ticket["hash"]
    try:
        session.unload()
    except Exception:  # noqa: BLE001 - best-effort teardown
        pass


def _world(paths):
    """Everything a fired effect would have changed."""
    def read(p):
        return Path(p).read_text(encoding="utf-8") if os.path.exists(p) else None
    return {"target": os.path.exists(paths["target"]),
            "bak": os.path.exists(paths["target"] + ".bak"),
            "queue": read(paths["queue"]), "sink": read(paths["sink"])}


def _outcomes(arm):
    return [(s["method"], s["class"], s["outcome"]) for s in arm["steps"]]


@needs_cordis
def test_the_recorded_arm_reproduces_the_recording(recorded):
    session, _, ticket = recorded
    out = session.counterfactual(1, replace={"key": "ops", "method": "enqueue",
                                             "args": ["/nowhere", "m"]})
    assert out["reproducesRecording"] is True, out["notReproduced"]
    assert _outcomes(out["recorded"]) == [
        ("stash", "a", "executed"), ("enqueue", "b", "deferred"),
        ("shout", "c", "ticket"), ("shout", "c", "executed")]
    totals = out["recorded"]["totals"]
    assert totals["tickets"] == [ticket]
    assert totals["residue"] == ["announce"]
    assert totals["witnessed"] == 1 and totals["deferred"] == ["deliver"]
    assert out["recorded"]["steps"][0]["witnessed"] == [
        {"extern": "stash_path", "inverse": "unstash"}]
    assert out["liveEffects"] == 0 and out["bounds"]


@needs_cordis
def test_replacing_a_witnessed_action_with_an_emission_tickets_it(recorded):
    session, paths, _ = recorded
    before_world = _world(paths)
    before_hash = session.commit()["hash"]
    out = session.counterfactual(0, replace={"key": "ops", "method": "shout",
                                             "args": [paths["sink"], "other"]})
    step = out["variant"]["steps"][0]
    assert (step["class"], step["outcome"]) == ("c", "ticket")
    assert out["divergence"]["first"] == 0
    assert out["divergence"]["downstream"] == []
    assert len(out["delta"]["tickets"]["added"]) == 1
    assert out["delta"]["witnessed"] == -1
    # nothing ran and the session is as it was
    assert _world(paths) == before_world
    assert session.commit()["hash"] == before_hash
    assert len(session.commit()["actions"]) == 4


@needs_cordis
def test_spending_the_recorded_yes_earlier_tickets_the_recorded_reissue(recorded):
    session, paths, ticket = recorded
    out = session.counterfactual(0, replace={"key": "ops", "method": "shout",
                                             "args": [paths["sink"], "hi"]})
    assert out["variant"]["steps"][0]["outcome"] == "ticket", \
        "the yes was minted after action 2, so it cannot cover action 0"
    out = session.counterfactual(3, insert={"key": "ops", "method": "shout",
                                            "args": [paths["sink"], "hi"]})
    inserted, reissue = out["variant"]["steps"][3], out["variant"]["steps"][4]
    assert (inserted["outcome"], inserted["approvalSpent"]) == ("executed", ticket)
    assert (reissue["outcome"], reissue["ticket"]) == ("ticket", ticket)
    [moved] = out["divergence"]["downstream"]
    assert moved["recorded"]["seq"] == 3 and "outcome" in moved["changed"]
    assert out["delta"]["residue"] == {"added": [], "removed": []}


@needs_cordis
def test_dropping_the_approved_reissue_leaves_its_yes_unused(recorded):
    session, _, ticket = recorded
    out = session.counterfactual(3, drop=True)
    assert out["substitution"] == {"kind": "drop", "action": None}
    assert out["variant"]["totals"]["unusedApprovals"] == [ticket]
    assert out["delta"]["residue"]["removed"] == ["announce"]
    assert out["divergence"]["first"] == 3


@needs_cordis
def test_a_step_covered_by_a_standing_grant_is_not_reproduced(monkeypatch,
                                                              tmp_path):
    session = _session(tmp_path)
    try:
        sink = str(tmp_path / "sink.log")
        session.mint_standing_grant(capability="announce", uses=1)
        session.act("ops", "shout", [sink, "hi"])
        out = session.counterfactual(0, drop=True)
        assert out["reproducesRecording"] is False
        [miss] = out["notReproduced"]
        assert miss["seq"] == 0 and "standing grant" in miss["why"]
        assert out["standingGrantsMinted"] == 1
    finally:
        session.unload()


@needs_cordis
def test_the_counterfactual_runs_nothing(recorded, monkeypatch):
    session, _, _ = recorded

    def refuse(*_args, **_kwargs):
        raise AssertionError("a counterfactual must not reach the runtime")

    with monkeypatch.context() as patched:   # undone before the teardown unloads
        patched.setattr(session, "call", refuse)
        patched.setattr(session, "_run", refuse)
        out = session.counterfactual(2, drop=True)
    assert out["liveEffects"] == 0


@needs_cordis
def test_the_mcp_verb_answers_and_refuses_a_bad_question(recorded):
    def ask(arguments):
        return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "revl_counterfactual",
                                         "arguments": arguments}}
                             )["result"]["structuredContent"]

    out = ask({"at": 3, "drop": True})
    assert out["ok"] is True and out["divergence"]["first"] == 3
    for bad in ({"at": 9, "drop": True}, {"at": 0},
                {"at": 0, "drop": True, "replace": {"key": "ops", "method": "x"}},
                {"at": 0, "replace": {"method": "shout"}}):
        refused = ask(bad)
        assert refused["ok"] is False, bad


@needs_cordis
def test_without_an_act_log_there_is_nothing_to_ask(tmp_path):
    from revl.mcp.session import SessionError

    session = _session(tmp_path)
    try:
        with pytest.raises(SessionError, match="revl_act log"):
            session.counterfactual(0, drop=True)
    finally:
        session.unload()


def test_revl_counterfactual_is_advertised_read_only():
    tool = next(t for t in server._ADVERTISED if t["name"] == "revl_counterfactual")
    assert tool["annotations"]["readOnlyHint"] is True
    from revl.mcp.runtime_gate import RUNTIME_VERBS
    assert "revl_counterfactual" in RUNTIME_VERBS

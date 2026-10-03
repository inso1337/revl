"""`revl_act`: one call per agent action through the approval gate (issue #1708).

A harness proposes an action and gets back its effect class and what happened:
a witnessed action executes and its receipt names the inverse that takes it
back; a deferred action is queued for commit; an immediate action returns a
ticket and fires nothing until an operator approves it. Every outcome is a
receipt, and `revl_commit`'s manifest lists them all.

Reuses the item-246 fixture: `stash` is class (a), `enqueue` class (b), `shout`
class (c). Needs the cordis-py runtime, like every session test.
"""

import copy
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl import compile_source  # noqa: E402
from revl.mcp import server  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="acting needs a live cordis-py composition; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)

SOURCE = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    "extern pure fn unstash(w: Stash) -> Unit = @py {\n"
    "    import os\n"
    "    if os.path.exists(w['bak']):\n"
    "        os.replace(w['bak'], w['path'])\n"
    "    return\n"
    "}\n"
    "extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError]"
    " undo unstash(result) = @py {\n"
    "    import os\n"
    "    bak = p + '.bak'\n"
    "    os.replace(p, bak)\n"
    "    return Ok({'path': p, 'bak': bak})\n"
    "}\n"
    "extern emission deferred fn deliver(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('deliver:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('announce:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service Ops {\n"
    "  emission fn stash(p: Str)\n"
    "  emission fn enqueue(sink: Str, msg: Str)\n"
    "  emission fn shout(sink: Str, msg: Str)\n"
    "}\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn stash(p) { effect stash_path(p) }\n"
    "    fn enqueue(sink, msg) { emit deliver(sink, msg) }\n"
    "    fn shout(sink, msg) { emit announce(sink, msg) }\n"
    "  }\n"
    "}\n"
)


@pytest.fixture
def gated(monkeypatch, tmp_path):
    """A recording session under the gate, as the compiler server's SESSION."""
    from revl.mcp.session import Session

    session = Session()
    session._wal_path = str(tmp_path / "session.wal")
    session.approval_policy = "auto"
    session.load(copy.deepcopy(compile_source(SOURCE, "act.rvl")), record=True)
    monkeypatch.setattr(server, "SESSION", session)
    yield session
    try:
        if session.loaded:
            session.unload()
    except Exception:  # noqa: BLE001 - best-effort teardown
        pass


@pytest.fixture
def paths(tmp_path):
    target = tmp_path / "artifact.txt"
    target.write_text("deliverable", encoding="utf-8")
    return {"target": str(target), "queue": str(tmp_path / "queue.log"),
            "shout": str(tmp_path / "shout.log")}


def _act(method, args):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "revl_act", "arguments": {
                              "key": "ops", "method": method, "args": args}}}
                         )["result"]["structuredContent"]


def _lines(path):
    return Path(path).read_text(encoding="utf-8").splitlines() \
        if os.path.exists(path) else []


@needs_cordis
def test_a_witnessed_action_executes_and_is_recorded_with_its_inverse(gated, paths):
    out = _act("stash", [paths["target"]])
    assert out["ok"] is True, out
    assert (out["class"], out["outcome"], out["residue"]) == ("a", "executed", [])
    assert os.path.exists(paths["target"] + ".bak"), "the action executed"
    receipt = out["receipt"]
    assert receipt["outcome"] == "executed" and receipt["class"] == "a"
    [witnessed] = receipt["witnessed"]
    assert witnessed["inverse"] == "unstash"
    assert witnessed["walSeq"] is not None, "durable in the session WAL"
    wal = [json.loads(line) for line in
           Path(gated._wal_path).read_text(encoding="utf-8").splitlines()
           if line.strip()]
    assert any(r.get("seq") == witnessed["walSeq"] for r in wal)


@needs_cordis
def test_a_deferred_action_appears_in_the_commit_manifest(gated, paths):
    out = _act("enqueue", [paths["queue"], "m"])
    assert out["ok"] is True, out
    assert (out["class"], out["outcome"]) == ("b", "deferred")
    assert _lines(paths["queue"]) == [], "deferred to commit, not fired"
    assert out["receipt"]["deferred"][0]["group"] == "deliver.deliver"
    manifest = server._tool_commit({})["manifest"]
    assert [d["group"] for d in manifest["deferred"]] == ["deliver.deliver"]
    assert manifest["actions"][0]["outcome"] == "deferred"


@needs_cordis
def test_an_immediate_action_returns_a_ticket_until_an_operator_approves(gated,
                                                                        paths):
    asked = _act("shout", [paths["shout"], "hi"])
    assert asked["ok"] is False and asked["approvalRequired"] is True
    assert (asked["class"], asked["outcome"]) == ("c", "ticket")
    assert asked["receipt"]["ticket"] == asked["ticket"]["hash"]
    assert _lines(paths["shout"]) == [], "a ticket fires nothing"

    gated.approve_ticket(asked["ticket"]["hash"])
    fired = _act("shout", [paths["shout"], "hi"])
    assert fired["ok"] is True, fired
    assert (fired["class"], fired["outcome"]) == ("c", "executed")
    assert _lines(paths["shout"]) == ["announce:hi"]
    assert [r["crossing"] for r in fired["residue"]] == ["announce"]


@needs_cordis
def test_the_commit_manifest_lists_all_three(gated, paths):
    _act("stash", [paths["target"]])
    _act("enqueue", [paths["queue"], "m"])
    ticket = _act("shout", [paths["shout"], "hi"])["ticket"]["hash"]
    out = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "revl_commit", "arguments": {}}})
    manifest = out["result"]["structuredContent"]["manifest"]
    rows = [(a["seq"], a["method"], a["class"], a["outcome"])
            for a in manifest["actions"]]
    assert rows == [(0, "stash", "a", "executed"), (1, "enqueue", "b", "deferred"),
                    (2, "shout", "c", "ticket")]
    assert manifest["actions"][2]["ticket"] == ticket
    assert manifest["witnessed"]["count"] == 1
    # the list does not move the hash: confirming still flushes what was listed
    done = server._tool_commit_confirm({"hash": manifest["hash"]})
    assert done["ok"] is True and done["committed"] is True
    assert _lines(paths["queue"]) == ["deliver:m"]
    assert _lines(paths["shout"]) == []


@needs_cordis
def test_without_the_gate_acting_is_refused(monkeypatch, tmp_path):
    from revl.mcp.session import Session

    session = Session()
    session.load(copy.deepcopy(compile_source(SOURCE, "act.rvl")))
    monkeypatch.setattr(server, "SESSION", session)
    try:
        out = _act("stash", [str(tmp_path / "x")])
        assert out["ok"] is False
        assert "approval gate" in out["diagnostics"][0]["message"]
    finally:
        session.unload()


def test_revl_act_is_advertised_and_gated_as_a_call():
    from revl.mcp.operator import TOOL_VERB
    from revl.mcp.runtime_gate import RUNTIME_VERBS

    names = [t["name"] for t in server._ADVERTISED]
    assert "revl_act" in names
    assert TOOL_VERB["revl_act"] == "call"
    assert "revl_act" in RUNTIME_VERBS


@needs_cordis
def test_an_operators_no_is_recorded_as_refused(gated, paths):
    asked = _act("shout", [paths["shout"], "hi"])
    out = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "revl_revoke", "arguments": {
                             "hash": asked["ticket"]["hash"]}}})
    assert out["result"]["structuredContent"]["ok"] is True
    refused = _act("shout", [paths["shout"], "hi"])
    assert refused["ok"] is False and refused["outcome"] == "refused"
    assert _lines(paths["shout"]) == []
    outcomes = [a["outcome"] for a in gated.commit()["actions"]]
    assert outcomes == ["ticket", "refused"]


@needs_cordis
def test_the_cli_form_runs_the_loop_and_prints_the_manifest(tmp_path, paths):
    import subprocess

    composition = tmp_path / "act.rvl"
    composition.write_text(SOURCE, encoding="utf-8")
    actions = [{"key": "ops", "method": "stash", "args": [paths["target"]]},
               {"key": "ops", "method": "enqueue", "args": [paths["queue"], "m"]},
               {"key": "ops", "method": "shout", "args": [paths["shout"], "hi"]}]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), env.get("PYTHONPATH", "")])
    done = subprocess.run(
        [sys.executable, "-P", "-m", "revl", "act", str(composition),
         "--wal", str(tmp_path / "act.wal"), "--commit"],
        input="".join(json.dumps(a) + "\n" for a in actions),
        capture_output=True, text=True, env=env, timeout=300)
    assert done.returncode == 0, done.stderr
    lines = [json.loads(line) for line in done.stdout.splitlines()]
    assert [(r["class"], r["outcome"]) for r in lines[:3]] == [
        ("a", "executed"), ("b", "deferred"), ("c", "ticket")]
    final = lines[3]
    assert [a["outcome"] for a in final["manifest"]["actions"]] == [
        "executed", "deferred", "ticket"]
    assert final["committed"]["committed"] is True
    assert _lines(paths["queue"]) == ["deliver:m"]
    assert _lines(paths["shout"]) == [], "the CLI cannot approve a ticket"

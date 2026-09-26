"""Issue #1444: a `load` that does not succeed leaves the session as it found it.

A refused `revl_load` used to leave the refused composition in the server's
module-level `SESSION` (`ir`, `_driver`, `_generation`, `_owner`, `origin`, the
surface epoch), and its host bodies in `_AUTHORED_HOST_BODIES` and
`_LIVE_HOST_BODIES`. The caller was told its load failed, and every later load
in the same process then failed with "a composition is already loaded".

Every test here drives the PRODUCT scenario (one process, one session, a load
that fails, then a load that must work) rather than a test ordering, and every
"nothing changed" assertion compares by VALUE against a copy taken before the
call. Comparing keys, or comparing against the same container object, would
pass while a value was replaced or a container was mutated in place.

`_clock_floor_ms` is the one field allowed to move: the session clock is
ratcheted (roadmap 427 F8) and a failed load must not be a way to wind it back.
`test_the_only_field_a_plain_refusal_moves_is_the_clock_floor` pins that.
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

import revl.mcp.server as server
from revl.mcp import admit_bridge, reflect_bridge
from revl.mcp.approval import ApprovalRequired

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a load boots a live cordis-py composition")

#: Refused by the py emitter by name: a `validated` extern is not a crossing
#: that tier can check. Compiles fine, so the refusal happens inside
#: `Session.load`, after the gates, which is where the state used to leak.
REFUSED = """
type Call = { tool: Str, args: Str }
type AgentTurn = Final(Str) | ToolCalls(List[Call])

extern emission[model] validated fn complete(h: Str) -> AgentTurn = @py {
  return {"kind": "Final", "value": "x"}
}

service Counter {
  fn next() -> Int
}

component CounterSvc provides counter: Counter {
  provide counter {
    fn next() = 42
  }
}
"""

GOOD = """
service Counter {
  fn next() -> Int
}

component CounterSvc provides counter: Counter {
  provide counter {
    fn next() = 7
  }
}
"""

REFUSAL = "the py emitter refused this composition"
_CLOCK_FLOOR = "_clock_floor_ms"


def _activation_emitter(sink: str) -> str:
    """A class-(c) emission in an ACTIVATION body, so under an approval policy
    the load itself raises the ticket two-step."""
    return (
        "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
        "    with open(sink, 'a') as _f:\n"
        "        _f.write('announce:' + msg + '\\n')\n"
        "    return\n"
        "}\n"
        "service Ops { fn ping() -> Int }\n"
        "component Quiet provides ops: Ops {\n"
        f"  emit announce(\"{sink}\", \"boot\")\n"
        "  provide ops { fn ping() = 1 }\n"
        "}\n"
    )


def _call(name: str, arguments: dict) -> dict:
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _copy(value):
    # one level down, so an in-place mutation of a container is a difference
    return copy.copy(value) if isinstance(value, (dict, list, set)) else value


def _globals() -> dict:
    """Everything a load can touch, copied so a later comparison is by value."""
    return {
        "SESSION": {k: _copy(v) for k, v in vars(server.SESSION).items()},
        "_AUTHORED_HOST_BODIES": copy.deepcopy(server._AUTHORED_HOST_BODIES),
        "_LIVE_HOST_BODIES": copy.deepcopy(server._LIVE_HOST_BODIES),
        "admit_bridge": admit_bridge.current(),
        "reflect_bridge": reflect_bridge.current(),
    }


def _same(a, b) -> bool:
    return a is b or a == b


def _differences(before: dict, after: dict, *, allow=()) -> list:
    out = []
    for name in ("_AUTHORED_HOST_BODIES", "_LIVE_HOST_BODIES",
                 "admit_bridge", "reflect_bridge"):
        if not _same(before[name], after[name]):
            out.append(name)
    was, now = before["SESSION"], after["SESSION"]
    for field in sorted(set(was) | set(now)):
        if field in allow:
            continue
        if field not in was or field not in now or not _same(was[field], now[field]):
            out.append(f"SESSION.{field}")
    return out


@pytest.fixture(autouse=True)
def _isolated():
    saved_authoring = server.AUTHORING
    saved_policy = server.SESSION.approval_policy
    if server.SESSION.loaded:
        _call("revl_unload", {})
    try:
        yield
    finally:
        server.AUTHORING = saved_authoring
        server.SESSION.approval_policy = saved_policy
        if server.SESSION.loaded:
            _call("revl_unload", {})


def _good_load_then_unload() -> None:
    """The first two steps of the issue's scenario: the process has already
    loaded and unloaded once, so the session is a used one, not a fresh one."""
    assert _call("revl_load", {"source": GOOD})["ok"] is True
    assert _call("revl_unload", {})["ok"] is True


def test_a_good_load_after_a_refused_load_succeeds():
    """The product scenario from the issue, end to end over the transport."""
    server.set_authoring_trust(host_code=True)
    _good_load_then_unload()
    before = _globals()

    refused = _call("revl_load", {"source": REFUSED})
    assert refused["ok"] is False
    assert refused["diagnostics"][0]["category"] == "session"
    assert REFUSAL in refused["diagnostics"][0]["message"]

    assert _differences(before, _globals(), allow={_CLOCK_FLOOR}) == []
    assert server.SESSION.ir is None
    assert server._LIVE_HOST_BODIES == []   # nothing is loaded, so nothing is live

    loaded = _call("revl_load", {"source": GOOD})
    assert loaded["ok"] is True, loaded
    assert _call("revl_call", {"key": "counter", "method": "next"})["result"] == 7


def test_the_only_field_a_plain_refusal_moves_is_the_clock_floor():
    """The allowance above, pinned from both sides: the floor may only move
    forward, and nothing else in the session may move at all."""
    server.set_authoring_trust(host_code=True)
    _good_load_then_unload()
    before = _globals()
    assert _call("revl_load", {"source": REFUSED})["ok"] is False
    after = _globals()
    assert _differences(before, after) in ([], [f"SESSION.{_CLOCK_FLOOR}"])
    assert after["SESSION"][_CLOCK_FLOOR] >= before["SESSION"][_CLOCK_FLOOR]


def _fault_at_emission(monkeypatch):
    from revl.mcp.session import Session

    def faulting(self, driver, ir):
        raise AttributeError("'NoneType' object has no attribute 'get'")
    monkeypatch.setattr(Session, "_emit_or_refuse", faulting)


def _fault_in_activation(monkeypatch):
    from revl import run

    async def faulting(self, ir, module):
        raise RuntimeError("a runtime fault while the composition was booting")
    monkeypatch.setattr(run._Driver, "_load", faulting)


@pytest.mark.parametrize("fault", [_fault_at_emission, _fault_in_activation],
                         ids=["at-emission", "in-activation"])
def test_a_load_that_faults_changes_nothing(monkeypatch, fault):
    """A FAULT is not a refusal (it reaches the transport as `internal`), but it
    is still a load that did not happen, so it must leave the same nothing."""
    server.set_authoring_trust(host_code=True)
    _good_load_then_unload()
    before = _globals()

    fault(monkeypatch)
    faulted = _call("revl_load", {"source": GOOD})
    assert faulted["ok"] is False
    assert faulted["diagnostics"][0]["category"] == "internal"
    monkeypatch.undo()

    assert _differences(before, _globals(), allow={_CLOCK_FLOOR}) == []
    assert _call("revl_load", {"source": GOOD})["ok"] is True


def test_a_field_nobody_listed_is_rolled_back_too(monkeypatch):
    """The rollback is of the whole session namespace, not of a list of fields,
    so a field added after this fix is covered without anyone updating it. Two
    shapes a list would miss: a brand-new attribute, and an in-place mutation
    of a container that already existed."""
    from revl.mcp.session import Session

    original = Session._install_cache_index

    def install_and_leak(self, ir):
        original(self, ir)
        self._a_field_added_after_1444 = "the refused composition"
        self._auto_reviewed["a-rule-nobody-reviewed"] = frozenset({"CounterSvc"})
    monkeypatch.setattr(Session, "_install_cache_index", install_and_leak)

    server.set_authoring_trust(host_code=True)
    before = _globals()
    assert _call("revl_load", {"source": REFUSED})["ok"] is False
    monkeypatch.undo()

    assert not hasattr(server.SESSION, "_a_field_added_after_1444")
    assert "a-rule-nobody-reviewed" not in server.SESSION._auto_reviewed
    assert _differences(before, _globals(), allow={_CLOCK_FLOOR}) == []


def test_every_survivor_names_a_real_field():
    """The fields a failed load deliberately leaves alone must exist, or a
    rename would silently turn an exemption into a rollback."""
    from revl.mcp.session import _SURVIVES_A_FAILED_LOAD, Session

    fresh = vars(Session())
    assert sorted(set(_SURVIVES_A_FAILED_LOAD) - set(fresh)) == []


def test_an_approval_a_failed_load_spent_stays_spent(tmp_path, monkeypatch):
    """The other direction. The activation gate spends an approval durably
    BEFORE the activation body can fire it (consume-before-fire), so a load that
    faults after the spend must not hand the yes back: the crossing may already
    have happened. The composition is rolled back; the spend is not."""
    from revl.compiler import compile_source
    from revl.mcp.session import Session

    sink = str(tmp_path / "sink.log")
    ir = compile_source(_activation_emitter(sink), "quiet.rvl")
    session = Session()
    session.approval_policy = "auto"

    with pytest.raises(ApprovalRequired) as asked:
        session.load(ir, record=True)
    ticket = asked.value.ticket
    session.approve_ticket(ticket["hash"])

    _fault_in_activation(monkeypatch)
    with pytest.raises(RuntimeError, match="runtime fault"):
        session.load(ir, record=True)
    monkeypatch.undo()

    assert session.ir is None and not session.loaded
    assert [e["consumed"] for e in session._ledger] == [True]
    # the yes is gone, so the same load asks again rather than booting on it
    with pytest.raises(ApprovalRequired) as again:
        session.load(ir, record=True)
    assert again.value.ticket["hash"] == ticket["hash"]
    assert not Path(sink).exists()


def test_a_failed_load_does_not_refund_an_auto_approve_budget(tmp_path, monkeypatch):
    """`_auto_spend` is created on a rule's FIRST materialization, which can be
    the failed load itself. Rolling it back would drop the entry, and the next
    load would rebuild it from the rule text with its whole budget: one free use
    per failed load, for ever."""
    from revl.compiler import compile_source
    from revl.mcp.session import Session
    from revl.policy import AutoApproveRule, Policy

    sink = str(tmp_path / "sink.log")
    ir = compile_source(
        "extern emission fn gwsend(host: Str, body: Str) = @py {\n"
        f"    with open({sink!r}, 'a') as _f:\n"
        "        _f.write('send:' + host + chr(10))\n"
        "    return\n"
        "}\n"
        "component Boot {\n"
        '  emit gwsend("api.stripe.com", "boot")\n'
        "}\n", "boot.rvl")
    session = Session()
    session.approval_policy = "auto"
    session.sandbox = Policy(auto_approve_rules=(AutoApproveRule(
        component="*", caps=('gwsend(host="api.stripe.com")',), realm=None,
        admitting=frozenset(), uses=2),))

    _fault_in_activation(monkeypatch)
    with pytest.raises(RuntimeError, match="runtime fault"):
        session.load(copy.deepcopy(ir), record=True)   # the gate spent use 1 of 2
    monkeypatch.undo()
    assert not session.loaded
    assert [s["remainingUses"] for s in session._auto_spend.values()] == [1]

    session.load(copy.deepcopy(ir), record=True)       # use 2 of 2, and it boots
    assert [s["remainingUses"] for s in session._auto_spend.values()] == [0]
    session.unload()
    with pytest.raises(ApprovalRequired):
        session.load(copy.deepcopy(ir), record=True)   # the bound is total
    assert Path(sink).read_text(encoding="utf-8").splitlines() == [
        "send:api.stripe.com"]


def test_a_failed_load_cannot_wind_the_session_clock_back(monkeypatch):
    """The session clock is ratcheted (roadmap 427 F8) and a failed load reads
    it. Restoring the floor would let the next reading go below what the failed
    load already saw, and a failed load is something an agent can cause at
    will."""
    from revl.compiler import compile_source
    from revl.mcp.session import Session, SessionError

    now = [2_000_000_000_000]
    session = Session()
    session._clock_ms = lambda: now[0]
    before = session._now_ms()

    now[0] = before + 60_000                     # time passes during the load
    with pytest.raises(SessionError, match=REFUSAL):
        session.load(compile_source(REFUSED, "refused.rvl"))
    assert session._clock_floor_ms == before + 60_000

    now[0] = before                              # and then the clock is rewound
    assert session._now_ms() == before + 60_000


def test_a_load_time_ticket_still_names_the_candidates_host_code(tmp_path):
    """`_AUTHORED_HOST_BODIES` now changes only when a load succeeds, so a
    ticket raised by the load itself must get the candidate's host bodies some
    other way. Without them an operator approving the activation crossing is
    told `announce` and not told that arbitrary host code rides along."""
    server.set_authoring_trust(host_code=True)
    server.SESSION.approval_policy = "auto"
    sink = str(tmp_path / "sink.log")
    source = _activation_emitter(sink)
    before = _globals()

    payload = _call("revl_load", {"source": source, "record": True})
    assert payload.get("approvalRequired") is True, payload
    ticket = payload["ticket"]
    assert ticket["unreviewedHostCode"] == [
        {"extern": "announce", "classification": "emission", "backends": ["py"]}]
    assert server.SESSION.ir is None
    assert server._AUTHORED_HOST_BODIES == before["_AUTHORED_HOST_BODIES"]

    assert _call("revl_approve", {"hash": ticket["hash"]})["ok"] is True
    assert _call("revl_load", {"source": source, "record": True})["ok"] is True
    assert server._AUTHORED_HOST_BODIES == ticket["unreviewedHostCode"]
    assert Path(sink).read_text(encoding="utf-8").splitlines() == ["announce:boot"]

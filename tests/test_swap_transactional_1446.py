"""Issue #1446: a `swap` that does not succeed leaves the running composition
serving.

A refused `revl_swap` used to take down the composition it was meant to
replace. The py emitter's refusal was raised from `_prepare_module`, which ran
AFTER `_dispose_all` had torn the running generation down, and only a failed
activation was rolled back. So the caller was told the swap failed, and the next
call to the service it had been using failed too: "its provider is inactive".

Every test here drives the product scenario (one process, one session, a
composition that answers, a swap that fails, the same call again) and every
"nothing changed" assertion compares by VALUE against a copy taken before the
swap. `_clock_floor_ms` is the one session field allowed to move, and only
forward, for the reason `tests/test_load_transactional_1444.py` gives.
"""

from __future__ import annotations

import copy
import importlib.util
import sys

import pytest

import revl.mcp.server as server
from revl.mcp import admit_bridge, reflect_bridge

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a swap boots a live cordis-py composition")

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

#: The same shape, answering 8, so a swap that DID go through is visible.
SUCCESSOR = GOOD.replace("= 7", "= 8")

#: Refused by the py emitter by name: a `validated` extern is not a crossing
#: that tier can check. It compiles and admits, so the refusal happens inside
#: `Session.swap`, which is where the running composition used to be lost.
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

REFUSAL = "the py emitter refused this composition"
_CLOCK_FLOOR = "_clock_floor_ms"


def _call(name: str, arguments: dict) -> dict:
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _next() -> dict:
    return _call("revl_call", {"key": "counter", "method": "next"})


def _copy(value):
    # one level down, so an in-place mutation of a container is a difference
    return copy.copy(value) if isinstance(value, (dict, list, set)) else value


def _globals() -> dict:
    """Everything a swap can touch, copied so a later comparison is by value:
    the session, the server's host-body records, the bridges, and the running
    generation's runtime (its driver, and the process-global module table and
    import path its emitted module lives in)."""
    driver = server.SESSION._driver
    prefix = driver._gen_prefix
    return {
        "SESSION": {k: _copy(v) for k, v in vars(server.SESSION).items()},
        "_AUTHORED_HOST_BODIES": copy.deepcopy(server._AUTHORED_HOST_BODIES),
        "_LIVE_HOST_BODIES": copy.deepcopy(server._LIVE_HOST_BODIES),
        "admit_bridge": admit_bridge.current(),
        "reflect_bridge": reflect_bridge.current(),
        "driver.generation": driver.generation,
        "driver.ir": driver.ir,
        "driver.fibers": dict(driver.fibers),
        "driver.emitted": driver.emitted,
        "sys.modules": {k: v for k, v in sys.modules.items()
                        if k.startswith(prefix)},
        "sys.path": list(sys.path),
    }


def _same(a, b) -> bool:
    return a is b or a == b


def _differences(before: dict, after: dict, *, allow=()) -> list:
    out = [name for name in before
           if name != "SESSION" and not _same(before[name], after[name])]
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


def _running() -> None:
    """The first two steps of the issue: a composition that answers."""
    server.set_authoring_trust(host_code=True)
    assert _call("revl_load", {"source": GOOD})["ok"] is True
    assert _next()["result"] == 7


def test_a_refused_swap_leaves_the_running_composition_answering():
    """The product scenario from the issue, end to end over the transport."""
    _running()
    before = _globals()

    refused = _call("revl_swap", {"source": REFUSED})
    assert refused["ok"] is False
    assert refused["diagnostics"][0]["category"] == "session"
    assert REFUSAL in refused["diagnostics"][0]["message"]

    assert _next()["result"] == 7
    after = _globals()
    assert _differences(before, after, allow={_CLOCK_FLOOR}) == []
    assert after["SESSION"][_CLOCK_FLOOR] >= before["SESSION"][_CLOCK_FLOOR]

    # and the session still swaps: the refusal left nothing half-done behind
    assert _call("revl_swap", {"source": SUCCESSOR})["ok"] is True
    assert _next()["result"] == 8


def _fault_in_the_emitter(monkeypatch):
    """A bug inside the emitter: a FAULT, not a refusal."""
    driver = server.SESSION._driver

    def faulting(ir):
        raise AttributeError("'NoneType' object has no attribute 'get'")
    monkeypatch.setattr(driver.emit, "emit", faulting)


def test_a_swap_whose_emitter_faults_changes_nothing(monkeypatch):
    """The issue left this unverified. A fault reaches the transport as
    `internal`, not `session`, but it is still a swap that did not happen, and
    it is raised before the teardown, so it must leave the same nothing."""
    _running()
    before = _globals()

    _fault_in_the_emitter(monkeypatch)
    faulted = _call("revl_swap", {"source": SUCCESSOR})
    monkeypatch.undo()
    assert faulted["ok"] is False
    assert faulted["diagnostics"][0]["category"] == "internal"

    assert _next()["result"] == 7
    assert _differences(before, _globals(), allow={_CLOCK_FLOOR}) == []


def _fault_once(monkeypatch, attribute: str, fault) -> None:
    """Make `run._Driver.<attribute>` fault on its next call only, so the
    successor fails and the rollback's reboot of the predecessor does not."""
    from revl import run

    original = getattr(run._Driver, attribute)
    fired = []

    def once(self, *args, **kwargs):
        if not fired:
            fired.append(attribute)
            return fault()
        return original(self, *args, **kwargs)
    monkeypatch.setattr(run._Driver, attribute, once)


def _fault_while_plugging(monkeypatch):
    def fault():
        raise KeyError("a fault while the successor module was plugged")
    _fault_once(monkeypatch, "_emit_module", fault)


def _fault_while_booting(monkeypatch):
    async def fault():
        raise RuntimeError("a runtime fault while the successor was booting")
    _fault_once(monkeypatch, "_load", fault)


@pytest.mark.parametrize("fault", [_fault_while_plugging, _fault_while_booting],
                         ids=["while-plugging", "while-booting"])
def test_a_swap_that_faults_after_the_teardown_is_rolled_back(monkeypatch, fault):
    """Past the teardown the running generation is gone, so it cannot be left
    untouched: it is rebooted, as a failed activation always was (item 372).
    It used to be only a failed activation. Any other fault left the session
    pointing at the successor with nothing providing.

    The rollback is a reboot, so it is recorded as a generation of its own, and
    the reboot gets a fresh session owner (item 245). Everything that says WHAT
    is running is what it was."""
    _running()
    before = _globals()["SESSION"]

    fault(monkeypatch)
    faulted = _call("revl_swap", {"source": SUCCESSOR})
    monkeypatch.undo()
    assert faulted["ok"] is False
    assert faulted["diagnostics"][0]["category"] == "internal"

    assert _next()["result"] == 7
    after = _globals()["SESSION"]
    for field in ("ir", "origin", "previous", "previous_origin", "_class_map",
                  "_surface_epoch", "_tickets"):
        assert _same(before[field], after[field]), field
    assert after["_history"][-1]["ir"] == before["ir"]   # the reboot, recorded
    assert _call("revl_swap", {"source": SUCCESSOR})["ok"] is True
    assert _next()["result"] == 8


def test_a_field_nobody_listed_is_rolled_back_on_a_refused_swap(monkeypatch):
    """The rollback before the teardown is the same whole-namespace checkpoint
    a failed load uses, so a field added later is covered without anyone
    listing it: a brand-new attribute, and an in-place change to a container
    that already existed."""
    from revl.mcp.session import Session

    original = Session._check_cache_applicability

    def check_and_leak(self, ir, class_map):
        original(self, ir, class_map)
        self._a_field_added_after_1446 = "the refused composition"
        self._auto_reviewed["a-rule-nobody-reviewed"] = frozenset({"CounterSvc"})

    _running()
    before = _globals()
    monkeypatch.setattr(Session, "_check_cache_applicability", check_and_leak)
    assert _call("revl_swap", {"source": REFUSED})["ok"] is False
    monkeypatch.undo()

    assert not hasattr(server.SESSION, "_a_field_added_after_1446")
    assert "a-rule-nobody-reviewed" not in server.SESSION._auto_reviewed
    assert _differences(before, _globals(), allow={_CLOCK_FLOOR}) == []
    assert _next()["result"] == 7


def test_a_ticket_a_swap_raised_stays_answerable(tmp_path):
    """The other direction, through `_SURVIVES_A_FAILED_LOAD`. A swap whose
    activation reaches a class-(c) emission answers with a ticket BEFORE the
    teardown, and that is a swap that did not happen too. The checkpoint must
    not take the ticket back with it, or `revl_approve` could never answer it.
    The ticket still names the candidate's host code, and the server records
    that host code as authored only once the swap goes through."""
    sink = tmp_path / "sink.log"
    candidate = (
        "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
        "    with open(sink, 'a') as _f:\n"
        "        _f.write('announce:' + msg + '\\n')\n"
        "    return\n"
        "}\n" + SUCCESSOR.replace(
            "provide counter {",
            f'emit announce("{sink}", "boot")\n  provide counter {{'))
    _running()
    server.SESSION.approval_policy = "auto"
    before = _globals()

    asked = _call("revl_swap", {"source": candidate})
    assert asked.get("approvalRequired") is True, asked
    ticket = asked["ticket"]
    assert ticket["unreviewedHostCode"] == [
        {"extern": "announce", "classification": "emission", "backends": ["py"]}]
    assert _next()["result"] == 7
    assert server._AUTHORED_HOST_BODIES == before["_AUTHORED_HOST_BODIES"]
    assert ticket["hash"] in server.SESSION._tickets

    assert _call("revl_approve", {"hash": ticket["hash"]})["ok"] is True
    assert _call("revl_swap", {"source": candidate})["ok"] is True
    assert _next()["result"] == 8
    assert server._AUTHORED_HOST_BODIES == ticket["unreviewedHostCode"]
    assert sink.read_text(encoding="utf-8").splitlines() == ["announce:boot"]


def test_a_refused_swap_cannot_wind_the_session_clock_back(monkeypatch):
    """The session clock is ratcheted (roadmap 427 F8), and a refused swap is
    something an agent can cause at will, so the checkpoint leaves the floor
    where a gate that ran during the refused swap moved it."""
    from revl.compiler import compile_source
    from revl.mcp.session import Session, SessionError

    now = [2_000_000_000_000]
    session = Session()
    session._clock_ms = lambda: now[0]
    session.load(compile_source(GOOD, "good.rvl"))
    original = Session._check_cache_applicability

    def a_gate_that_reads_the_clock(self, ir, class_map):
        self._now_ms()
        original(self, ir, class_map)
    monkeypatch.setattr(Session, "_check_cache_applicability",
                        a_gate_that_reads_the_clock)
    try:
        start = session._now_ms()
        now[0] = start + 60_000                  # time passes during the swap
        with pytest.raises(SessionError, match=REFUSAL):
            session.swap(compile_source(REFUSED, "refused.rvl"))
        assert session._clock_floor_ms == start + 60_000

        now[0] = start                           # and then the clock is rewound
        assert session._now_ms() == start + 60_000
        assert session.call("counter", "next", {})["result"] == 7
    finally:
        session.unload()


# ------------------------------------- the ticket names the candidate's code

#: A running composition that carries one agent-authored host body, `alpha`.
RUNNING_HOST_CODE = (
    "extern pure fn alpha(x: Str) -> Str = @py {\n"
    "    return x\n"
    "}\n" + GOOD)

_ANNOUNCE = (
    "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
    "    return\n"
    "}\n")
_BOOT_EMIT = 'emit announce("s", "boot")\n  provide counter {'


def _entry(name: str, classification: str) -> dict:
    return {"extern": name, "classification": classification, "backends": ["py"]}


def _running_host_code() -> None:
    server.set_authoring_trust(host_code=True)
    assert _call("revl_load", {"source": RUNNING_HOST_CODE})["ok"] is True
    assert _next()["result"] == 7
    server.SESSION.approval_policy = "auto"


def test_a_swap_ticket_names_the_candidates_host_code_not_the_running_one():
    """The ticket's `unreviewedHostCode` is what a yes lets run. While a
    composition runs, it used to name the RUNNING composition's host code
    (`alpha`) and omit the candidate's new emission (`announce`) and pure
    body (`beta`), so the operator approved code they were never shown."""
    _running_host_code()
    candidate = (_ANNOUNCE
                 + "extern pure fn beta(x: Str) -> Str = @py {\n"
                   "    return x\n"
                   "}\n"
                 + GOOD.replace("provide counter {", _BOOT_EMIT))

    asked = _call("revl_swap", {"source": candidate})
    assert asked.get("approvalRequired") is True, asked
    ticket = asked["ticket"]
    assert ticket["unreviewedHostCode"] == [
        _entry("announce", "emission"), _entry("beta", "pure")]
    assert ticket["newHostCode"] == [
        _entry("announce", "emission"), _entry("beta", "pure")]
    assert ticket["runningHostCode"] == [_entry("alpha", "pure")]
    assert "`runningHostCode` is what runs now" in \
        ticket["unreviewedHostCodeWarning"]
    assert ticket["hash"] in server.SESSION._tickets   # the fields ride a copy
    assert _next()["result"] == 7


def test_new_host_code_is_what_the_running_composition_does_not_run_already():
    """`newHostCode` compares by content: a body carried over byte for byte is
    not new, and one that keeps its name but changes its text is."""
    _running_host_code()
    changed_alpha = RUNNING_HOST_CODE.replace("    return x\n",
                                              "    return x + x\n")
    kept = _ANNOUNCE + RUNNING_HOST_CODE.replace("provide counter {", _BOOT_EMIT)
    changed = _ANNOUNCE + changed_alpha.replace("provide counter {", _BOOT_EMIT)

    ticket = _call("revl_swap", {"source": kept})["ticket"]
    assert ticket["unreviewedHostCode"] == [
        _entry("alpha", "pure"), _entry("announce", "emission")]
    assert ticket["newHostCode"] == [_entry("announce", "emission")]

    ticket = _call("revl_swap", {"source": changed})["ticket"]
    assert ticket["newHostCode"] == [
        _entry("alpha", "pure"), _entry("announce", "emission")]


def test_an_edit_ticket_names_the_edited_host_code():
    """`revl_edit` swaps through the same `Session.swap`, and its ticket
    reaches the server's catch-all rather than `_tool_swap`, so the candidate
    has to travel on the ticket itself."""
    _running_host_code()
    asked = _call("revl_edit", {"edits": [
        {"anchor": "service Counter {", "replacement": _ANNOUNCE + "service Counter {"},
        {"anchor": "provide counter {", "replacement": _BOOT_EMIT},
    ]})
    assert asked.get("approvalRequired") is True, asked
    ticket = asked["ticket"]
    assert ticket["unreviewedHostCode"] == [
        _entry("alpha", "pure"), _entry("announce", "emission")]
    assert ticket["newHostCode"] == [_entry("announce", "emission")]
    assert ticket["runningHostCode"] == [_entry("alpha", "pure")]
    assert _next()["result"] == 7


def test_a_ticket_with_nothing_running_lists_no_running_code():
    """A first load replaces nothing, so its ticket carries the candidate's
    host code and neither of the replacement lists."""
    server.set_authoring_trust(host_code=True)
    server.SESSION.approval_policy = "auto"
    candidate = _ANNOUNCE + GOOD.replace("provide counter {", _BOOT_EMIT)
    asked = _call("revl_load", {"source": candidate, "record": True})
    assert asked.get("approvalRequired") is True, asked
    ticket = asked["ticket"]
    assert ticket["unreviewedHostCode"] == [_entry("announce", "emission")]
    assert "newHostCode" not in ticket and "runningHostCode" not in ticket

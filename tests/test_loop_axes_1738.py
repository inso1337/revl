"""`revl_state` reports the six agent-loop axes (issue #1738).

The exit tests: a scripted MCP session with a known mix reports exactly the
expected six values, and a session with no approval policy still reports all
six. The mix is two witnessed effects, one deferred, one ticketed, one
preflighted edit, one change refused at admission, and one aborted change that
leaves one residue.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.loop_axes import LoopAxes, carried_components, covered_components  # noqa: E402
from revl.mcp.server import SESSION, handle  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the loop axes are measured against a live cordis-py composition; "
           "install it with `sh backends/python/setup.sh` and run under its venv",
)

AXES = ("reversibilityRate", "autoApprovedWithProof", "promptsPerSession",
        "preflightCoverage", "violationsCaughtBeforeExecution", "residueAfterAbort")

# `stash` is class (a), a witnessed rename with a registered inverse;
# `enqueue` is class (b), a deferred emission; `pay` is class (c), an
# emission whose compensation fails when the abort runs it, which is the
# residue. `Echo` is the component the preflighted edit replaces.
_HEAD = (
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
    "extern emission fn charge(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('charge:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "extern emission fn refund_fails(sink: Str, msg: Str) = @py {\n"
    "    raise RuntimeError('the refund endpoint is down')\n"
    "}\n"
    "service Ops {\n"
    "  emission fn stash(p: Str)\n"
    "  emission fn enqueue(sink: Str, msg: Str)\n"
    "  emission fn pay(sink: Str, msg: Str)\n"
    "}\n"
    "service Echo { fn say(m: Str) -> Str }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn stash(p) { effect stash_path(p) }\n"
    "    fn enqueue(sink, msg) { emit deliver(sink, msg) }\n"
    "    fn pay(sink, msg) { emit charge(sink, msg) compensate refund_fails(sink, msg) }\n"
    "  }\n"
    "}\n"
)
_ECHO = "component EchoBox provides echo: Echo {{\n  provide echo {{\n    fn say(m) = {body}\n  }}\n}}\n"
_SOURCE = _HEAD + _ECHO.format(body="m")
_EDITED = _HEAD + _ECHO.format(body='"echo"')
# A second provider of `echo` in the same realm: G2 refuses it at admission.
_REFUSED = _SOURCE + _ECHO.format(body='"twice"').replace("EchoBox", "EchoTwin")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture
def mcp(tmp_path):
    """The process-wide MCP session with a fresh set of loop counters, the
    composition files under a root the operator sanctioned, and everything put
    back afterwards."""
    old_policy, old_authoring = SESSION.approval_policy, server_mod.AUTHORING
    old_axes = SESSION._loop_axes
    SESSION._loop_axes = LoopAxes()
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    paths = {}
    for name, text in (("base", _SOURCE), ("edited", _EDITED), ("refused", _REFUSED)):
        paths[name] = tmp_path / f"{name}.rvl"
        paths[name].write_text(text, encoding="utf-8")
    for i in range(2):
        (tmp_path / f"artifact_{i}.txt").write_text(f"deliverable {i}", encoding="utf-8")
    try:
        yield tmp_path, paths
    finally:
        if SESSION.loaded:
            _call("revl_unload", {})
        SESSION.approval_policy = old_policy
        server_mod.AUTHORING = old_authoring
        SESSION._loop_axes = old_axes


def _values(axes: dict) -> dict:
    return {name: (axes[name]["numerator"], axes[name]["denominator"],
                   axes[name]["value"]) for name in AXES}


def _run_the_mix(tmp_path, paths, policy):
    SESSION.approval_policy = policy
    sink = str(tmp_path / "sink.log")
    assert _call("revl_load", {"files": [str(paths["base"])], "record": True})["ok"]
    for i in range(2):                                  # two witnessed effects
        assert _call("revl_call", {"key": "ops", "method": "stash",
                                   "args": [str(tmp_path / f"artifact_{i}.txt")]})["ok"]
    assert _call("revl_call", {"key": "ops", "method": "enqueue",
                               "args": [sink, "later"]})["ok"]       # one deferred
    paid = _call("revl_call", {"key": "ops", "method": "pay", "args": [sink, "x"]})
    if policy is not None:                              # one ticketed
        assert paid.get("approvalRequired") is True
        assert _call("revl_approve", {"hash": paid["ticket"]["hash"]})["ok"]
        paid = _call("revl_call", {"key": "ops", "method": "pay", "args": [sink, "x"]})
    assert paid["ok"], paid
    # one preflighted edit: ask what EchoBox reaches, then replace it
    assert _call("revl_query_reach", {"files": [str(paths["base"])],
                                      "component": "EchoBox"})["ok"]
    swapped = _call("revl_swap", {"files": [str(paths["edited"])],
                                  "replacing": ["EchoBox"]})
    assert swapped.get("swapped") is True, swapped
    # one change refused at admission, naming G2
    refused = _call("revl_swap", {"files": [str(paths["refused"])]})
    assert refused["ok"] is False
    assert any(d.get("guarantee") for d in refused["diagnostics"]), refused
    # one aborted change, whose failing compensation is the one residue
    aborted = _call("revl_abort", {})
    assert aborted["ok"] and len(aborted["compensationResidue"]) == 1, aborted
    return _call("revl_state", {})


@needs_cordis
def test_a_scripted_session_reports_the_six_expected_values(mcp):
    tmp_path, paths = mcp
    state = _run_the_mix(tmp_path, paths, policy="auto")
    assert _values(state["loopAxes"]) == {
        "reversibilityRate": (2, 4, 0.5),           # stash, stash of a, b, b, c
        "autoApprovedWithProof": (3, 4, 0.75),      # a, a, b; the c needed a yes
        "promptsPerSession": (2, 1, 2.0),           # pay's ticket + the residue
        "preflightCoverage": (1, 1, 1.0),           # the swap was queried first
        "violationsCaughtBeforeExecution": (1, 1, 1.0),
        "residueAfterAbort": (1, 1, 1.0),
    }
    assert state["loopAxes"]["boundaryCalls"] == {"a": 2, "b": 1, "c": 1,
                                                  "unclassified": 0}


@needs_cordis
def test_a_session_with_no_approval_policy_still_reports_all_six(mcp):
    tmp_path, paths = mcp
    state = _run_the_mix(tmp_path, paths, policy=None)
    assert "approval" not in state            # the policy block stays policy-only
    assert _values(state["loopAxes"]) == {
        "reversibilityRate": (2, 4, 0.5),
        "autoApprovedWithProof": (3, 4, 0.75),
        "promptsPerSession": (1, 1, 1.0),           # no ticket; the residue prompts
        "preflightCoverage": (1, 1, 1.0),
        "violationsCaughtBeforeExecution": (1, 1, 1.0),
        "residueAfterAbort": (1, 1, 1.0),
    }


def test_the_axes_are_reported_before_anything_is_loaded(mcp):
    state = _call("revl_state", {})
    assert state["loaded"] is False
    assert set(AXES) <= set(state["loopAxes"])
    for name in AXES:
        assert state["loopAxes"][name] == {"numerator": 0, "denominator": 0,
                                           "value": None}


@needs_cordis
def test_an_edit_with_no_query_first_is_not_preflighted(mcp):
    tmp_path, paths = mcp
    assert _call("revl_load", {"files": [str(paths["base"])]})["ok"]
    assert _call("revl_swap", {"files": [str(paths["edited"])],
                               "replacing": ["EchoBox"]}).get("swapped") is True
    axes = _call("revl_state", {})["loopAxes"]
    assert axes["preflightCoverage"] == {"numerator": 0, "denominator": 1, "value": 0.0}


# No host code, so it loads from inline source under the default trust.
_CHAIN = (
    "service Store { fn get(k: Str) -> Opt[Str] }\n"
    "service Front { fn hit(k: Str) -> Opt[Str] }\n"
    "component Mem provides store: Store {\n"
    "  let m = effect Map.new() undo m.drop()\n"
    "  provide store { fn get(k) = m.get(k) }\n"
    "}\n"
    "component Web requires store: Store provides front: Front {\n"
    "  provide front { fn hit(k) = store.get(k) }\n"
    "}\n"
)


@needs_cordis
def test_an_edit_that_carries_its_cascade_is_preflighted(mcp):
    """#1704 (PR #1731): a `revl_edit` response carries `blastRadius`, the
    exact cascade of what it touched, so the agent had the preflight answer
    without asking for it first. It also covers a later edit of the same
    component, as an earlier query would."""
    assert _call("revl_load", {"source": _CHAIN})["ok"]
    edited = _call("revl_edit", {"edits": [{
        "anchor": "fn hit(k) = store.get(k)",
        "replacement": "fn hit(k) = store.get(\"front:\" + k)"}]})
    assert edited["swapped"] is True, edited
    assert edited["blastRadius"]["touched"] == ["Web"]
    axes = _call("revl_state", {})["loopAxes"]
    assert axes["preflightCoverage"] == {"numerator": 1, "denominator": 1, "value": 1.0}


@needs_cordis
def test_a_failing_call_is_a_refusal_at_run_time(mcp, tmp_path):
    _, paths = mcp
    assert _call("revl_load", {"files": [str(paths["base"])]})["ok"]
    missing = str(tmp_path / "no-such-file.txt")
    failed = _call("revl_call", {"key": "ops", "method": "stash", "args": [missing]})
    assert failed["ok"] is False
    axes = _call("revl_state", {})["loopAxes"]
    assert axes["violationsCaughtBeforeExecution"] == {
        "numerator": 0, "denominator": 1, "value": 0.0}


def test_query_coverage_reads_every_result_shape():
    assert covered_components({"component": "A"}, {"cascade": [{"component": "B"}]}) \
        == {"A", "B"}
    assert covered_components({}, {"components": ["C"], "impacted": ["D"],
                                   "providers": [{"component": "E"}],
                                   "callSites": [{"component": "F"}]}) \
        == {"C", "D", "E", "F"}
    assert covered_components({}, {"components": {"added": ["G"], "replaced": ["H"],
                                                  "withdrawn": []}}) == {"G", "H"}


def test_carried_cascade_reads_touched_and_each_cascade():
    payload = {"blastRadius": {"touched": ["Mem"], "components": {
        "Mem": {"cascade": [{"component": "Web"}, {"component": "Client"}]}}}}
    assert carried_components(payload) == {"Mem", "Web", "Client"}
    assert carried_components({"swapped": True}) == set()

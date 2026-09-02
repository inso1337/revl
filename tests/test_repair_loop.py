"""The repair loop — faults that fix themselves, within policy (roadmap item 62).

The loop reimplements no machinery; it orchestrates the landed pieces (why 27,
bisect 40, gauntlet 31, policy 33, reuse 49, swap 23, widening-ack 21, operator
authority 55). So the tests split the same way the gauntlet's do: the
orchestration, the policy gate, the widening-ack, and the dossier reconstruction
run everywhere (admission is pure frontend); only the *unattended swap* of the
exit test needs the cordis-py runtime, and carries the `@needs_runtime` marker.

The pinned exit test (`test_injected_fault_repaired_unattended`) is the roadmap's
own: an injected fault in a demo composition is detected, repaired
(regenerate -> gauntlet -> policy -> swap), verified, and swapped with ZERO human
input, and the incident report reconstructs every step from the causal trace.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import why_runtime as wr  # noqa: E402
from revl.mcp import repair as _repair  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

# a two-component demo composition: a cache with a bug in `size()`
DEMO = """
service Cache { fn get(key: Str) -> Opt[Str]
                fn size() -> Int }
component MemCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key)
                  fn size() = 0 }
}
"""

# the regenerated repair: same boundary (reaches nothing on G8), the fix is
# internal — so it is admissible against the running composition and WIDENS
# nothing. This is the candidate the unattended loop swaps.
REPAIR = DEMO  # structurally identical boundary; a real fix changes only bodies

# a repair that reaches host code the running composition does not — its extern
# lands on the G8 boundary as a NEW crossing, so it WIDENS the composition's
# outward reach and must stop for a human ack (item 21).
WIDENING_REPAIR = """
extern pure fn now_ms() -> Int = @py { import time; return int(time.time()*1000) }
service Cache { fn get(key: Str) -> Opt[Str]
                fn size() -> Int }
component MemCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key)
                  fn size() = now_ms() }
}
"""

# a self-repair policy that authorizes MemCache and lets a repair touch anything
POLICY_OPEN = {"eligible": [{"component": "MemCache"}], "mayTouch": None,
               "ackOnWiden": True}


needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the unattended swap needs the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`, then run under "
           "`backends/python/.venv/bin/pytest`",
)


@pytest.fixture(autouse=True)
def _fresh_session():
    from revl.mcp import server as server_mod
    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()


@pytest.fixture
def trusted_authoring():
    """`WIDENING_REPAIR` DECLARES a new `@py` host body, so it is agent-authored
    host code, which the MCP server refuses under its default (closed) authoring
    trust — before the widening-ack ever runs. Acknowledging a new CROSSING is
    not the same authority as trusting the agent to write the BODY behind it, so
    the widening tests state the trusted-author premise they always relied on
    (`server.AuthoringTrust`, `revl mcp serve --author-trust trusted`)."""
    from revl.mcp import server as server_mod
    before = server_mod.AUTHORING
    server_mod.set_authoring_trust(host_code=True)
    yield
    server_mod.AUTHORING = before


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _fault_trace(component: str, detail: str) -> list:
    """An item-27 causal trace of `component` faulting: it loaded at boot, then
    withdrew on an injected trigger."""
    return [
        wr.make_event(0, 0, wr.LOAD, component, "PENDING -> ACTIVE",
                      wr.cause_boot()),
        wr.make_event(1, 0, wr.WITHDRAW, component, "ACTIVE -> FAILED",
                      wr.cause_trigger(detail)),
    ]


# ====================================================================
# the self-repair policy (this module's own contribution)
# ====================================================================


def test_self_repair_policy_json_and_dsl_agree():
    from_json = _repair.parse_self_repair_policy(
        {"eligible": [{"component": "Cache*"}, {"realm": "edge"}],
         "mayTouch": ["kv", "log*"], "ackOnWiden": True})
    from_dsl = _repair.parse_self_repair_policy(
        "component Cache* may self-repair\n"
        "realm edge may self-repair\n"
        "self-repair may touch kv, log*\n")
    assert from_json.eligibility == from_dsl.eligibility
    assert from_json.may_touch == from_dsl.may_touch
    assert from_json.ack_on_widen == from_dsl.ack_on_widen


def test_eligibility_is_by_component_or_realm_glob():
    p = _repair.parse_self_repair_policy({"eligible": [{"component": "Cache*"}]})
    assert p.eligible("Cache1", frozenset()) is not None
    assert p.eligible("Other", frozenset()) is None
    r = _repair.parse_self_repair_policy({"eligible": [{"realm": "edge"}]})
    assert r.eligible("X", frozenset({"edge"})) is not None
    assert r.eligible("X", frozenset({"core"})) is None


def test_may_touch_bounds_capabilities_and_star_is_literal_only():
    p = _repair.parse_self_repair_policy({"mayTouch": ["kv", "log*"]})
    assert p.out_of_bounds(["kv", "logStore"]) == []
    assert p.out_of_bounds(["kv", "net"]) == ["net"]
    # an unnameable reach `*` is only ever in-bounds by a literal `*`
    assert p.out_of_bounds(["*"]) == ["*"]
    assert _repair.parse_self_repair_policy({"mayTouch": ["*"]}).out_of_bounds(["*"]) == []
    # None means inherit: nothing is out of bounds here (widening handles it)
    assert _repair.parse_self_repair_policy({}).out_of_bounds(["net"]) == []


def test_may_widen_turns_the_ack_off():
    p = _repair.parse_self_repair_policy("component X may self-repair\n"
                                         "self-repair may widen\n")
    assert p.ack_on_widen is False


# ====================================================================
# the verb, and the halts that need no runtime (admission is pure frontend)
# ====================================================================


def test_the_verb_is_advertised():
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in listed["result"]["tools"]}
    assert "revl_repair" in names


def test_component_is_required():
    d = _call("revl_repair", {})
    assert d["ok"] is False


def test_no_policy_is_closed_by_default():
    # eligible only if a policy names it; with none, nothing self-repairs
    d = _call("revl_repair", {"component": "MemCache",
                              "candidate": {"source": REPAIR}})
    assert d["incident"]["status"] == _repair.STATUS_INELIGIBLE
    assert d["eligibility"]["eligible"] is False


def test_ineligible_component_halts_before_touching_anything():
    d = _call("revl_repair", {
        "component": "MemCache", "candidate": {"source": REPAIR},
        "selfRepairPolicy": {"eligible": [{"component": "Other"}]}})
    assert d["incident"]["status"] == _repair.STATUS_INELIGIBLE
    assert d["remediation"] is None


def test_no_candidate_and_no_reuse_is_no_candidate():
    d = _call("revl_repair", {"component": "MemCache",
                              "selfRepairPolicy": POLICY_OPEN})
    assert d["incident"]["status"] == _repair.STATUS_NO_CANDIDATE


def test_a_candidate_that_fails_admission_is_rejected_not_thrown():
    bad = REPAIR.replace("fn size() = 0", 'fn size() = "nope"')
    d = _call("revl_repair", {"component": "MemCache",
                              "candidate": {"source": bad},
                              "selfRepairPolicy": POLICY_OPEN})
    assert d["ok"] is True
    assert d["incident"]["status"] == _repair.STATUS_REJECTED
    assert d["verdicts"]["gauntlet"]["verdict"] == "rejected"
    assert d["remediation"]["applied"] is False


def test_may_touch_bound_refuses_a_repair_that_reaches_too_far(trusted_authoring):
    # eligible, admissible, but the repair reaches `now_ms` and the policy caps
    # the repair to `kv` — a refusal (not an ack): the bound is absolute.
    d = _call("revl_repair", {
        "component": "MemCache", "candidate": {"source": WIDENING_REPAIR},
        "selfRepairPolicy": {"eligible": [{"component": "MemCache"}],
                             "mayTouch": ["kv"]}})
    assert d["incident"]["status"] == _repair.STATUS_REJECTED
    assert "now_ms" in d["verdicts"]["mayTouch"]["outOfBounds"]
    assert d["remediation"]["blockedBy"] == "may-touch"


def test_the_fault_why_is_reconstructed_from_the_trace():
    d = _call("revl_repair", {
        "component": "MemCache",
        "trace": _fault_trace("MemCache", "injected fault: size() stale"),
        "candidate": {"source": REPAIR}, "selfRepairPolicy": POLICY_OPEN})
    fault = d["fault"]
    assert fault["recorded"] is True
    assert fault["root"]["cause"]["kind"] == wr.TRIGGER
    assert "injected fault" in fault["root"]["cause"]["detail"]


# ====================================================================
# the exit test — the unattended loop, end to end
# ====================================================================


@needs_runtime
def test_injected_fault_repaired_unattended():
    """THE ROADMAP EXIT TEST. An injected fault in a demo composition is
    detected, repaired (regenerate -> gauntlet -> policy -> swap), verified, and
    swapped with ZERO human input, and the incident report reconstructs every
    step from the causal trace alone."""
    loaded = _call("revl_load", {"source": DEMO, "record": True})
    assert loaded["ok"] is True

    d = _call("revl_repair", {
        "component": "MemCache",
        # the fault, as its item-27 causal trace
        "trace": _fault_trace("MemCache",
                              "injected fault: cache.size() returned a stale count"),
        # the fault localized to a step (item 40)
        "predicate": "step >= 0",
        # the regenerated repair
        "candidate": {"source": REPAIR},
        # authorized, unattended, within bounds
        "selfRepairPolicy": POLICY_OPEN,
    })

    # (1) repaired and swapped with zero human input
    assert d["ok"] is True
    assert d["incident"]["status"] == _repair.STATUS_REPAIRED
    assert d["incident"]["swapped"] is True
    assert d["incident"]["unattended"] is True
    assert d["remediation"]["strategy"] == "swap"
    assert d["remediation"]["applied"] is True

    # (2) every gate was crossed, in order, and is recorded
    assert d["fault"]["recorded"] is True                      # why (27)
    assert d["verdicts"]["gauntlet"]["verdict"] == "admissible"  # gauntlet (31)
    assert d["verdicts"]["widening"]["widened"] is False       # no widening (21)
    assert d["eligibility"]["eligible"] is True                # self-repair policy

    # (3) authority: the self-repair policy authorized it, no human in the loop
    assert d["authority"]["authority"] == "self-repair-policy"
    assert d["authority"]["applied"] is True
    assert d["authority"]["why"]["kind"] == "self-repair-authority"

    # (4) the incident report reconstructs EVERY step from the causal trace
    steps = {s["stage"]: s for s in d["dossier"]["steps"]}
    for stage in ("fault", "eligibility", "candidate", "gauntlet", "policy",
                  "widening", "swap", "authority", "incident"):
        assert stage in steps, f"missing dossier step: {stage}"
    # the load-bearing ones actually happened
    assert steps["fault"]["reached"] is True
    assert steps["gauntlet"]["reached"] is True
    assert steps["swap"]["reached"] is True
    assert steps["authority"]["reached"] is True

    # (5) the running composition is now the repaired generation
    state = _call("revl_state", {})
    assert state["generation"] >= 1


@needs_runtime
def test_a_widening_repair_pauses_for_a_human_ack(trusted_authoring):
    """The human-ack interrupt (item 21): a candidate that would WIDEN the
    composition's outward reach stops the loop instead of auto-swapping."""
    _call("revl_load", {"source": DEMO, "record": True})
    baseline = _call("revl_state", {})["generation"]
    d = _call("revl_repair", {
        "component": "MemCache",
        "trace": _fault_trace("MemCache", "injected fault"),
        "candidate": {"source": WIDENING_REPAIR},
        "selfRepairPolicy": POLICY_OPEN,
    })
    assert d["incident"]["status"] == _repair.STATUS_AWAITING_ACK
    assert d["incident"]["swapped"] is False
    widen = d["verdicts"]["widening"]
    assert widen["widened"] is True
    assert any("now_ms" in c for c in widen["unacknowledged"])
    assert d["authority"]["pendingAck"] is True
    # the pause is recorded in the dossier
    widening_step = next(s for s in d["dossier"]["steps"] if s["stage"] == "widening")
    assert widening_step["reached"] is False
    # and the running composition is untouched (no new generation swapped in)
    assert _call("revl_state", {})["generation"] == baseline


@needs_runtime
def test_acknowledging_the_widening_lets_the_swap_proceed(trusted_authoring):
    """The same widening repair, with the crossing acknowledged, swaps — the ack
    is the human input the awaiting-ack status was waiting for."""
    _call("revl_load", {"source": DEMO, "record": True})
    d = _call("revl_repair", {
        "component": "MemCache",
        "trace": _fault_trace("MemCache", "injected fault"),
        "candidate": {"source": WIDENING_REPAIR},
        "selfRepairPolicy": POLICY_OPEN,
        "accept": ["host:MemCache:now_ms"],
    })
    assert d["incident"]["status"] == _repair.STATUS_REPAIRED
    assert d["incident"]["swapped"] is True


@needs_runtime
def test_plan_runs_every_gate_but_does_not_swap():
    _call("revl_load", {"source": DEMO, "record": True})
    baseline = _call("revl_state", {})["generation"]
    d = _call("revl_repair", {
        "component": "MemCache",
        "trace": _fault_trace("MemCache", "injected fault"),
        "candidate": {"source": REPAIR}, "selfRepairPolicy": POLICY_OPEN,
        "apply": False})
    assert d["incident"]["status"] == _repair.STATUS_PLANNED
    assert d["incident"]["swapped"] is False
    assert d["verdicts"]["gauntlet"]["verdict"] == "admissible"
    assert _call("revl_state", {})["generation"] == baseline

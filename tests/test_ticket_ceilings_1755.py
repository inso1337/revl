"""The approval ticket carries the item-260 ceiling of each capability it asks
about (issue #1755, part 1).

An operator approving a class-(c) call, or minting a standing grant with
`uses=N`, sees how many times the crossing component can cross that capability:
`ceilings[cap] = {verdict, ceiling, reason}` from `revl.cardinality`. Under the
off-by-default policy line `approvals require bounded crossings`, a call whose
capability is `unbounded` is refused instead of ticketed, naming it.

Analysis only: no syntax, IR, emitter or self-host change, and the ticket hash
is unchanged (the field lands after it).
"""

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl.compiler import compile_source  # noqa: E402
from revl.mcp import server  # noqa: E402
from revl.mcp.approval import ClassMap  # noqa: E402
from revl.policy import parse_policy  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the ticket is raised by a live cordis-py session; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)

HEAD = """
type Action = Final(Str) | Search(Str) | Pay(Str)
service Model    { emission[model] fn next(h: List[Str]) -> Action }
service Search   { fn find(q: Str) -> Str }
service Payments { emission[charge] fn pay(k: Str) -> Str }
service Agent    { emission[model, charge] fn run(p: Str) -> Str }
component Brain provides model: Model {
  provide model { fn next(h) { return Final("done") } }
}
component Index provides search: Search { provide search { fn find(q) = q } }
component Till provides charge: Payments { provide charge { fn pay(k) = k } }
"""

#: the issue's multi-action agent loop, with history: a Pay action crosses
#: `charge` through the dispatch arrow
LOOP = """
fn agent_loop(h: List[Str], decide: (List[Str]) -> Action,
              act: (Action) -> Str, n: Int) -> Str {
  if (n <= 0) { return "max_steps" }
  let a = decide(h)
  return match a {
    Final(x) => x,
    _ => agent_loop(h.push(act(a)), decide, act, n - 1),
  }
}
"""

AGENT = """
component Agent requires model: Model, search: Search, charge: Payments
    provides agent: Agent {
  config { max_steps: Int = 5 }
  provide agent {
    fn run(p) {
      return agent_loop([p], DECIDE, ACT, config.max_steps)
    }
  }
}
"""

ACT = """a => match a {
          Search(q) => search.find(q),
          Pay(k) => emit charge.pay(k),
          Final(x) => x,
        }"""

#: the same loop with the emitting arrow hidden in a record field: the
#: cardinality escape rule reports `unbounded` instead of dropping the crossing
HIDDEN_LOOP = """
type Tools = { act: (Action) -> Str }
fn agent_loop(h: List[Str], decide: (List[Str]) -> Action,
              tools: Tools, n: Int) -> Str {
  if (n <= 0) { return "max_steps" }
  let a = decide(h)
  let act = tools.act
  return match a {
    Final(x) => x,
    _ => agent_loop(h.push(act(a)), decide, tools, n - 1),
  }
}
"""

ONCE = """
service Agent { emission[charge] fn run(p: Str) -> Str }
service Payments { emission[charge] fn pay(k: Str) -> Str }
component Till provides charge: Payments { provide charge { fn pay(k) = k } }
component Agent requires charge: Payments provides agent: Agent {
  provide agent { fn run(p) { return emit charge.pay(p) } }
}
"""


def _source(hidden: bool = False) -> str:
    agent = AGENT.replace("DECIDE", "h => emit model.next(h)")
    if hidden:
        return HEAD + HIDDEN_LOOP + agent.replace("ACT", "{ act: " + ACT + " }")
    return HEAD + LOOP + agent.replace("ACT", ACT)


def _ticket(source: str) -> dict:
    cm = ClassMap(compile_source(source, "t.rvl"))
    return cm.build_ticket(cm.classify_call("agent", "run"), ["go"])


def test_the_agent_loop_ticket_carries_a_symbolic_ceiling_for_charge():
    ticket = _ticket(_source())
    charge = ticket["ceilings"]["charge"]
    assert (charge["verdict"], charge["ceiling"]) == ("bounded-symbolic",
                                                      "config.max_steps")
    assert "config.max_steps" in charge["reason"]
    assert ticket["ceilings"]["model"]["verdict"] == "bounded-symbolic"


def test_a_hidden_arrow_reports_charge_unbounded_with_the_escape_reason():
    charge = _ticket(_source(hidden=True))["ceilings"]["charge"]
    assert charge["verdict"] == "unbounded" and charge["ceiling"] is None
    # the #1766 escape reason: the arrow is used other than by invoking it
    assert "other than by invoking it" in charge["reason"], charge["reason"]


def test_a_capability_crossed_once_reports_bounded_1():
    charge = _ticket(ONCE)["ceilings"]["charge"]
    assert (charge["verdict"], charge["ceiling"]) == ("bounded", 1)


def test_the_ceilings_do_not_move_the_ticket_hash():
    ir = compile_source(_source(), "t.rvl")
    cm = ClassMap(ir)
    reach = cm.classify_call("agent", "run")
    with_ceilings = cm.build_ticket(reach, ["go"])
    from revl.mcp import approval

    class NoCeilings(approval.ClassMap):
        def ceilings(self, reach, tokens):
            return {}
    without = NoCeilings(ir).build_ticket(reach, ["go"])
    assert with_ceilings["hash"] == without["hash"]


def test_the_policy_line_parses_and_is_off_by_default():
    assert parse_policy("approvals require bounded crossings\n").approvals_bounded
    assert not parse_policy("capability charge requires approval\n").approvals_bounded
    import json
    from revl.policy import _parse_json
    doc = json.dumps({"approvalCeilings": {"refuseUnbounded": True}})
    assert _parse_json(doc, None).approvals_bounded


@pytest.fixture
def gated(monkeypatch, tmp_path):
    from revl.mcp.session import Session

    def start(source, policy=None):
        session = Session()
        session._wal_path = str(tmp_path / "session.wal")
        session.approval_policy = "auto"
        if policy is not None:
            session.sandbox = parse_policy(policy)
        session.load(copy.deepcopy(compile_source(source, "t.rvl")), record=True)
        monkeypatch.setattr(server, "SESSION", session)
        started.append(session)
        return session

    started = []
    yield start
    for session in started:
        try:
            session.unload()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass


def _call():
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "revl_call", "arguments": {
                              "key": "agent", "method": "run", "args": ["go"]}}}
                         )["result"]["structuredContent"]


@needs_cordis
def test_revl_call_on_the_agent_loop_returns_the_ceiling_on_the_ticket(gated):
    gated(_source())
    out = _call()
    assert out.get("approvalRequired") is True, out
    charge = out["ticket"]["ceilings"]["charge"]
    assert (charge["verdict"], charge["ceiling"]) == ("bounded-symbolic",
                                                      "config.max_steps")


@needs_cordis
def test_with_the_flag_an_unbounded_capability_is_refused_by_name(gated):
    gated(_source(hidden=True), policy="approvals require bounded crossings\n")
    out = _call()
    assert out["ok"] is False and not out.get("approvalRequired"), out
    message = out["diagnostics"][0]["message"]
    assert "`charge`" in message and "unbounded" in message
    # nothing was ticketed: the refusal comes before the question is asked
    assert server.SESSION._tickets == {}


@needs_cordis
def test_without_the_flag_the_unbounded_call_is_still_only_ticketed(gated):
    gated(_source(hidden=True))
    out = _call()
    assert out.get("approvalRequired") is True
    assert out["ticket"]["ceilings"]["charge"]["verdict"] == "unbounded"


@needs_cordis
def test_with_the_flag_a_bounded_loop_is_ticketed_as_before(gated):
    gated(_source(), policy="approvals require bounded crossings\n")
    assert _call().get("approvalRequired") is True


def test_an_analysis_that_cannot_run_reads_unbounded(monkeypatch):
    import revl.cardinality as card

    def broken(ir):
        raise RuntimeError("boom")

    monkeypatch.setattr(card, "cardinality", broken)
    charge = _ticket(ONCE)["ceilings"]["charge"]
    assert charge["verdict"] == "unbounded"
    assert "did not run" in charge["reason"] and "boom" in charge["reason"]

"""A model's advice cannot lower an operator-owned approval floor (#2754).

One component takes the model-returned boolean, routes on it in a provide
method, and reaches the operator-required capability only from one branch.
The policy-owned `requires approval` binding is what admits that branch, and
the model's "no human needed" value cannot fire it: the crossing still pends
for the operator's approval and does not spend the typed approval early.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backends" / "python"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the typed-approval runtime is exercised through cordis-py; "
           "install it with `sh backends/python/setup.sh`",
)


def _router_source(*, approval_edge=True):
    """One component: the model stub, the branch on its boolean, the crossing.

    The false value is the model's "no human needed" advice. The operator's
    floor is the `Approval[pay.charge]` binding in the activation body; it is
    what admits the `pay.charge` crossing in the advice's false arm.
    """
    binding = (
        '  let token = await approval["pay.charge"] { amount: 1 }\n'
        if approval_edge else ""
    )
    crossing = (
        '        emit charge(sink) with token\n'
        if approval_edge else
        '        emit charge(sink)\n'
    )
    return (
        "extern emission[model.complete] fn model_classify() -> Bool = @py "
        "{ return False }\n"
        "extern emission[pay.charge] fn charge(sink: Str) = @py {\n"
        "    with open(sink, 'a') as f:\n"
        "        f.write('charged\\n')\n"
        "    return\n"
        "}\n"
        "service Router {\n"
        "  emission fn classify() -> Bool\n"
        "  emission fn dispatch(needs_human: Bool, sink: Str) -> Str\n"
        "}\n"
        "component Router provides router: Router {\n"
        + binding +
        "  provide router {\n"
        "    fn classify() { return emit model_classify() }\n"
        "    fn dispatch(needs_human, sink) {\n"
        "      if (needs_human == false) {\n"
        + crossing +
        '        return "charged"\n'
        "      }\n"
        '      return "escalated"\n'
        "    }\n"
        "  }\n"
        "}\n"
    )


@needs_cordis
def test_model_advice_does_not_lower_the_operator_approval_floor(tmp_path):
    from revl.compiler import compile_source
    from revl.mcp.approval import ApprovalRequired
    from revl.mcp.session import Session, SessionError
    from revl.policy import parse_policy

    ir = compile_source(_router_source(), "model_advice_approval_floor.rvl")
    sink = tmp_path / "charge.log"

    session = Session()
    session.sandbox = parse_policy("capability pay.charge requires approval")
    session.approval_policy = "auto"
    session._wal_path = str(tmp_path / "router.wal")
    # The operator's typed approval for the floor the policy names. The model
    # advice must neither replace it nor spend it.
    session.grant_language_approval(
        "pay.charge", "Router", fields={"amount": 1},
        candidate_hash=session._approval_candidate_hashes(ir)["Router"])
    session.load(ir, record=True)
    # The model call itself is an operator-approved class-(c) crossing.
    session.mint_standing_grant(capability="model.complete", uses=1)

    # The model answers "no human needed"; the value goes into the one
    # component, which routes on it in its own provide method.
    needs_human = session.call("router", "classify", [])["result"]
    assert needs_human is False

    with pytest.raises(ApprovalRequired) as pending:
        session.call("router", "dispatch", [needs_human, str(sink)])
    assert "pay.charge" in pending.value.ticket["capabilities"]
    assert not sink.exists()
    # The advice did not lower the floor: the operator's typed approval for
    # pay.charge is still unconsumed while the crossing waits for a human.
    assert session._owner.approval_ledger[0]["consumed"] is False

    session.approve_ticket(pending.value.ticket["hash"])
    result = session.call("router", "dispatch", [needs_human, str(sink)])
    assert result["result"] == "charged"
    assert sink.read_text(encoding="utf-8").splitlines() == ["charged"]
    assert session._owner.approval_ledger[0]["consumed"] is True

    # Acceptance for #2754: remove the `Approval[pay.charge]` binding from the
    # same component and the operator's floor refuses it at admission, so this
    # test fails rather than passing on an unbound crossing.
    unbound = Session()
    unbound.sandbox = parse_policy("capability pay.charge requires approval")
    with pytest.raises(SessionError, match="requires approval"):
        unbound.load(compile_source(_router_source(approval_edge=False),
                                    "model_advice_approval_floor_unbound.rvl"),
                     record=True)

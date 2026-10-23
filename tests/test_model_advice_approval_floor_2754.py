"""A model's advice cannot lower an operator-owned approval floor (#2754)."""

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


def _advisor_source():
    return (
        "extern emission[model.complete] fn model_classify() -> Bool = @py "
        "{ return False }\n"
        "service Advice {\n"
        "  emission fn classify() -> Bool\n"
        "  fn route(needs_human: Bool) -> Str\n"
        "}\n"
        "component Advisor provides advice: Advice {\n"
        "  provide advice {\n"
        "    fn classify() -> Bool { return emit model_classify() }\n"
        '    fn route(needs_human) -> Str {\n'
        '      if (needs_human == false) { return "pay.charge" }\n'
        '      else { return "skip" }\n'
        "    }\n"
        "  }\n"
        "}\n"
    )


def _charge_source(charge_sink, *, approval_edge=True):
    approval = (
        '  let a = await approval["pay.charge"] { amount: 1 }\n'
        if approval_edge else ""
    )
    crossing = (f'  emit charge("{charge_sink}") with a\n'
                if approval_edge else
                f'  emit charge("{charge_sink}")\n')
    return (
        "extern emission[pay.charge] fn charge(sink: Str) = @py {\n"
        "    with open(sink, 'a') as _f:\n"
        "        _f.write('charged\\n')\n"
        "    return\n"
        "}\n"
        "component Biller {\n"
        + approval + crossing +
        "}\n"
    )


@needs_cordis
def test_no_human_model_advice_does_not_lower_operator_approval_floor(tmp_path):
    from revl.compiler import compile_source
    from revl.mcp.approval import ApprovalRequired
    from revl.mcp.session import Session, SessionError
    from revl.policy import parse_policy

    charge = tmp_path / "charge.log"
    policy = "capability pay.charge requires approval"

    advisor = Session()
    advisor.approval_policy = "auto"
    advisor._wal_path = str(tmp_path / "advisor.wal")
    advisor.load(compile_source(
        _advisor_source(), "model_advice_approval_floor.rvl"), record=True)
    advisor.mint_standing_grant(capability="model.complete", uses=1)

    # The model returns its "no human needed" value. The caller routes that
    # advice to Biller, but the operator's approval floor still raises a ticket.
    needs_human = advisor.call("advice", "classify")["result"]
    assert needs_human is False
    route = advisor.call("advice", "route", [needs_human])["result"]
    assert route == "pay.charge"
    assert not charge.exists()

    protected = Session()
    protected.sandbox = parse_policy(policy)
    protected.approval_policy = "auto"
    protected._wal_path = str(tmp_path / "protected.wal")
    with pytest.raises(ApprovalRequired) as pending:
        if route == "pay.charge":
            protected.load(compile_source(
                _charge_source(charge), "model_advice_approval_floor.rvl"),
                record=True)
    assert "pay.charge" in pending.value.ticket["capabilities"]
    assert not charge.exists()

    # The policy also refuses the same route when its Approval[pay.charge]
    # binding is removed, so the floor cannot silently degrade to an unbound emit.
    unbound = Session()
    unbound.sandbox = parse_policy(policy)
    unbound._wal_path = str(tmp_path / "unbound-approval.wal")
    with pytest.raises(SessionError, match="requires approval"):
        unbound.load(compile_source(
            _charge_source(charge, approval_edge=False),
            "model_advice_approval_floor_unbound.rvl"), record=True)

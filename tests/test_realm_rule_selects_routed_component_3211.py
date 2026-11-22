"""A realm-scoped policy rule selects a component placed with `realms(...)`
exactly as it selects one placed with `realm(...)` (issue #3211).

`component_realms` once read only the `isolate` map, while a `realms(...)`
placement is lowered into `routes`. A routed component then had no realm and
fell outside every realm-scoped rule, so `realm w1 may reach llm` bounded
nothing for it. The capability leg (`may reach`), the deny leg
(`may not reach`) and the inert-deny selection warning each call
`component_realms`; each is pinned here for both spellings, the pinned
(`realm(...)`) one being the control.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.policy import (  # noqa: E402
    InertDenyPolicyWarning, evaluate, parse_policy)

SOURCE = """
service Ops { emission fn stash(p: Int) -> Int }
service Tool { emission fn run(p: Int) -> Int }
component OpsImpl provides ops: Ops {
  isolate ops in realm("w1")
  provide ops { fn stash(p) = p }
}
component ToolV2 requires ops: Ops provides tool: Tool {
  isolate ops in PLACEMENT
  provide tool { fn run(p) { emit ops.stash(p); return 1 } }
}
"""

PLACEMENTS = ['realm("w1")', 'realms("w1")']


def _audit(placement: str) -> dict:
    return audit_report(compile_source(
        SOURCE.replace("PLACEMENT", placement), "routed.rvl"))


def _violations(placement: str, rule: str):
    return evaluate(parse_policy(rule), _audit(placement))


@pytest.mark.parametrize("placement", PLACEMENTS)
def test_realm_allow_list_refuses_the_component_that_reaches_outside(placement):
    violations = _violations(placement, "realm w1 may reach llm")
    assert [(v.kind, v.component) for v in violations] == [
        ("capability", "ToolV2")], violations


@pytest.mark.parametrize("placement", PLACEMENTS)
def test_realm_allow_list_that_names_the_reach_is_clean(placement):
    assert _violations(placement, "realm w1 may reach *") == []


@pytest.mark.parametrize("placement", PLACEMENTS)
def test_realm_deny_rule_refuses_the_component_that_reaches_the_token(placement):
    violations = _violations(placement, "realm w1 may not reach *")
    assert [(v.kind, v.component) for v in violations] == [
        ("deny", "ToolV2")], violations


@pytest.mark.parametrize("placement", PLACEMENTS)
def test_inert_deny_warning_lists_the_routed_component_as_selected(placement):
    with pytest.warns(InertDenyPolicyWarning) as caught:
        violations = _violations(placement, "realm w1 may not reach ops*")
    assert violations == []
    selected = str(caught[0].message).split("it selected [")[1].split("]")[0]
    assert selected.split(", ") == ["OpsImpl", "ToolV2"], selected


def test_realms_placement_matches_the_pinned_placement():
    pinned = _violations(PLACEMENTS[0], "realm w1 may reach llm")
    routed = _violations(PLACEMENTS[1], "realm w1 may reach llm")
    assert [(v.kind, v.component) for v in routed] == [
        (v.kind, v.component) for v in pinned]


def test_realms_placement_across_two_realms_is_selected_by_either():
    source = SOURCE.replace("PLACEMENT", 'realms("w1", "w2")') + (
        'component OpsImplW2 provides ops: Ops {\n'
        '  isolate ops in realm("w2")\n'
        '  provide ops { fn stash(p) = p }\n'
        '}\n')
    audit = audit_report(compile_source(source, "routed2.rvl"))
    for realm in ("w1", "w2"):
        violations = evaluate(parse_policy(f"realm {realm} may reach llm"),
                              audit)
        assert [v.component for v in violations] == ["ToolV2"], (realm,
                                                                 violations)

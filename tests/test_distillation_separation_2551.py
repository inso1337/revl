"""Applying a distillation offer follows the approval separation rule
(issue #2551).

`revl_approve(capability=...)` refuses a standing grant when approval separation
is on and the caller cannot be a separate approver: no operator profile bound,
or a bound operator that may make calls (issue #1706, `_refuse_self_grant`).
`revl_apply_distillation` installs the same kind of rule, a live
`AutoApproveRule` in the bound policy, and used to reach the policy without that
check, so a standing auto-approve the direct route refuses could be installed
through the offer route.

Through the real dispatch (`handle()` on the session `revl mcp serve` binds with
no flag):

* control: with separation on and no operator bound, a direct standing grant is
  refused and the refusal says no operator profile is bound; the same holds for
  a bound operator that may make calls;
* gap, no operator bound: applying an offer is refused the same way, the policy
  gains no rule and nothing is recorded as applied;
* gap, operator that may make calls: applying an offer from its own ledger is
  refused, the offer stays listed and the policy gains no rule;
* positive controls: an operator granted `approve` but not `call` applies the
  offer and the rule is installed, and with separation off the offer applies
  with no operator, as before.

The two gap tests assert the fixed behaviour and are strict xfails until the fix
lands; the fix removes the markers. The no-operator case builds its offer by
stubbing the ledger fold (`_offer_by_id`), the way tests/test_1982_never_standing.py
and tests/test_1062_mint_bound.py do, because an unattributed ledger gives the
fold no author to name; the operator cases use a real seeded ledger.

Every test needs the cordis-py runtime and skips without it.
"""

import copy
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp.operator import parse_profile  # noqa: E402
from revl.mcp.server import handle  # noqa: E402
from revl.policy import AutoApproveRule  # noqa: E402
from test_approval_gate_default_1706 import (  # noqa: E402,F401
    _load_gated, _profile, _why, fresh)
from test_distill_enforcement import (  # noqa: E402,F401
    _seed_settled_ledger, _src, sink)

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="booting a composition needs the cordis-py runtime; install it with "
           "`sh backends/python/setup.sh` and run under its venv")

GAP = pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="issue #2551: apply_distillation skips the separation check")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _installed(session) -> tuple:
    """The standing auto-approve rules the bound policy carries."""
    return tuple(getattr(session.sandbox, "auto_approve_rules", ()) or ())


def _stub_offer(session, monkeypatch) -> str:
    """Make `offer-1` resolve to a settled offer for the `announce` capability of
    the `Agent` component the gated session loads. Returns the offer id."""
    rule = AutoApproveRule(component="Agent", caps=("announce",), uses=3)
    blast = types.SimpleNamespace(not_covered=(), covered=1, total=1)
    offer = types.SimpleNamespace(rule=rule, blast=blast, operator="",
                                  sessions=("s0", "s1"), grant_count=6,
                                  rule_text=rule.to_dsl())
    monkeypatch.setattr(session, "_offer_by_id", lambda offer_id: offer)
    return "offer-1"


def _bind(session, token: str) -> None:
    session.operator = parse_profile(_profile()).get(token)


def _offer_from_ledger(session, sink_path: str, token: str) -> str:
    """Load the billing composition under `token`, seed a settled ledger
    attributed to it, and return the id of the offer the ledger folds to."""
    session.approval_policy = "auto"
    session.approval_separation = True
    session.load(copy.deepcopy(compile_source(_src(sink_path), "biller.rvl")),
                 record=True)
    _bind(session, token)
    _seed_settled_ledger(session, operator=token)
    offers = session.distillation_offers()["offers"]
    assert offers, "the seeded ledger must fold to an offer"
    return offers[0]["offerId"]


# The scenarios are built in fixtures, so a setup failure is an error and never
# reads as the expected failure of a gap test.


@pytest.fixture
def unattributed_offer(fresh, monkeypatch):  # noqa: F811
    """Separation on, no operator bound, an offer the author side can apply."""
    _load_gated(fresh)
    return _stub_offer(fresh, monkeypatch)


@pytest.fixture
def ungated_offer(fresh, monkeypatch):  # noqa: F811
    """The same session with separation off."""
    _load_gated(fresh)
    fresh.approval_separation = False
    return _stub_offer(fresh, monkeypatch)


@pytest.fixture
def caller_offer(fresh, sink):  # noqa: F811
    """An offer from the ledger of an operator that may make calls."""
    return _offer_from_ledger(fresh, sink, "agent")


@pytest.fixture
def approver_offer(fresh, sink):  # noqa: F811
    """An offer from the ledger of an operator granted `approve` only."""
    return _offer_from_ledger(fresh, sink, "human")


# ------------------------------------------------------------------ controls


def test_a_direct_standing_grant_with_no_operator_bound_is_refused(fresh):  # noqa: F811
    _load_gated(fresh)
    refused = _call("revl_approve", {"capability": "announce", "uses": 3})
    assert refused["ok"] is False, refused
    assert "no operator profile is bound" in _why(refused), refused
    assert not fresh._grants


def test_a_direct_standing_grant_by_an_operator_that_may_call_is_refused(fresh):  # noqa: F811
    _load_gated(fresh)
    _bind(fresh, "agent")
    refused = _call("revl_approve", {"capability": "announce", "uses": 1})
    assert refused["ok"] is False, refused
    assert "may make calls" in _why(refused), refused
    assert not fresh._grants


# ---------------------------------------------------------------------- gaps


@GAP
def test_apply_with_no_operator_bound_is_refused_and_installs_nothing(
        fresh, unattributed_offer):  # noqa: F811
    verdict = _call("revl_apply_distillation", {"offerId": unattributed_offer})
    assert verdict["ok"] is False, verdict
    assert "no operator profile is bound" in _why(verdict), verdict
    assert not _installed(fresh)
    assert not fresh._auto_rules
    assert fresh._distillation_seq == 0


@GAP
def test_apply_by_an_operator_that_may_call_is_refused_and_the_offer_stays(
        fresh, caller_offer):  # noqa: F811
    offer_id = caller_offer
    verdict = _call("revl_apply_distillation", {"offerId": offer_id})
    assert verdict["ok"] is False, verdict
    assert "may make calls" in _why(verdict), verdict
    assert not _installed(fresh)
    assert not fresh._auto_rules
    listed = [o["offerId"] for o in fresh.distillation_offers()["offers"]]
    assert offer_id in listed


# ---------------------------------------------------------- positive controls


def test_apply_by_an_operator_that_cannot_call_installs_the_rule(
        fresh, approver_offer):  # noqa: F811
    verdict = _call("revl_apply_distillation", {"offerId": approver_offer})
    assert verdict["ok"] is True and verdict["applied"] is True, verdict
    assert len(_installed(fresh)) == 1
    assert len(fresh._auto_rules) == 1


def test_apply_with_separation_off_installs_the_rule_with_no_operator(
        fresh, ungated_offer):  # noqa: F811
    verdict = _call("revl_apply_distillation", {"offerId": ungated_offer})
    assert verdict["ok"] is True and verdict["applied"] is True, verdict
    assert len(_installed(fresh)) == 1
    assert len(fresh._auto_rules) == 1

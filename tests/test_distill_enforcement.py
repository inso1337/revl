"""Distilled `AutoApproveRule` ENFORCEMENT — roadmap item 251, Slice 2.

Slice 1 landed the pure distiller and the `AutoApproveRule` parse. Slice 2 records
the resource-scoped projections into the ledger and ENFORCES the distilled rules on
the SAME runtime consent path a hand-written rule is checked on (item 344's standing
grant). These tests pin the review-fix cases the design closes:

  * N1 (CRITICAL): a distilled `gwsend(host="api.stripe.com")` rule auto-approves a
    send to api.stripe.com but NOT to attacker.example — the resource scope is
    enforced by the same `cap_order.covers` order a hand-written rule is, and the
    ledger records the BOUND host, not an opaque `argsDigest` hash;
  * H1 (HIGH): a component ENTERING the rule's glob that was not in the reviewed
    blast set SUSPENDS the rule and re-prompts (fail-closed), never silently
    auto-approved (open-world glob membership is part of the suspend signature);
  * H2 (HIGH): the admission taint gate uses the static over-approximation as its
    floor; an UNKNOWN taint at admission is treated as ALL FIVE origins
    (fail-closed), NEVER an empty set a `{} subset admitting` test waves through;
  * apply / revoke are item-55 `approve`-gated and attributed with WAL records;
  * consume-before-fire holds (a uses-bounded rule cannot double-fire, and the
    spend is a durable `approval-consumed` record written before the fire);
  * a composition with no distilled rule is byte-identical (no policy applied).

Proven end-to-end through the live cordis-py runtime, like the item-344 suite.
"""

import copy
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from revl.mcp.approval import ApprovalRequired  # noqa: E402
from revl.mcp.operator import Operator, parse_profile  # noqa: E402
from revl.policy import AutoApproveRule, Policy  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="enforcement is proven against a live cordis-py composition — install "
           "it with `sh backends/python/setup.sh` and run under its venv",
)

STRIPE = "api.stripe.com"
ATTACKER = "attacker.example"


def _src(sink: str, component: str = "Biller", tainted: bool = False) -> str:
    """A component `component` providing `gw: Gw`; `gw.send(host, body)` emits the
    host extern `gwsend(host, body)`, whose `host` parameter is a `cap_order`
    resource kind, so the crossing carries `gwsend(host="...")`. With `tainted`,
    the body is fed from a web source, so the component's static taint reaches
    `web` (the H2 realistic case)."""
    src = (
        f"extern emission fn gwsend(host: Str, body: Str) = @py {{\n"
        f"    with open({sink!r}, 'a') as _f:\n"
        f"        _f.write('send:' + host + ':' + body + chr(10))\n"
        f"    return\n"
        f"}}\n"
    )
    if tainted:
        src += (
            'extern emission[web] fn fetch(url: Str) -> Untrusted[Str]'
            ' = @py { return "x" }\n'
            "service Gw { emission fn send(host: Str, body: Str) }\n"
            f"component {component} provides gw: Gw {{\n"
            "  provide gw { fn send(host, body) { emit gwsend(host, fetch(body)) } }\n"
            "}\n"
        )
    else:
        src += (
            "service Gw { emission fn send(host: Str, body: Str) }\n"
            f"component {component} provides gw: Gw {{\n"
            "  provide gw { fn send(host, body) { emit gwsend(host, body) } }\n"
            "}\n"
        )
    return src


def _session(rule: AutoApproveRule | None = None, *, operator: str | None = None,
             policy: object = "auto"):
    from revl.mcp.session import Session
    s = Session()
    s.approval_policy = policy
    if rule is not None:
        s.sandbox = Policy(auto_approve_rules=(rule,))
    if operator is not None:
        s.operator = Operator(operator)
    return s


def _rule(host: str = STRIPE, *, component: str = "Biller*",
          admitting=frozenset(), uses: int | None = 10) -> AutoApproveRule:
    return AutoApproveRule(component=component,
                           caps=(f'gwsend(host="{host}")',),
                           realm=None, admitting=admitting, uses=uses)


def _lines(sink: str) -> list:
    return Path(sink).read_text().splitlines() if os.path.exists(sink) else []


def _open_wal(session, tmp_path):
    """Open the approval WAL for a policy-gated (non-typed-approval) session so the
    consume-before-fire spend and the distillation records are durable — the
    operator/test controls the WAL location (`Session._wal_path`)."""
    session.recorder.open_wal(str(tmp_path / "approval.wal"), session._generation)


def _wal_records(session):
    wal_path = session.recorder.wal.path
    session.unload()
    return [json.loads(line)
            for line in Path(wal_path).read_text().splitlines()]


@pytest.fixture
def sink(tmp_path):
    return str(tmp_path / "sink.log")


# ---------------------------------------------------------------------------
# N1: the resource scope is enforced, and the ledger records the bound host
# ---------------------------------------------------------------------------

@needs_cordis
def test_n1_host_scoped_rule_auto_approves_only_the_named_host(sink):
    """A distilled `gwsend(host="api.stripe.com")` rule auto-approves a send to
    api.stripe.com but NOT a send to attacker.example — the resource scope is
    enforced by `covers`, exactly as a hand-written narrowing would be."""
    ir = compile_source(_src(sink), "biller.rvl")
    session = _session(_rule())
    session.load(copy.deepcopy(ir), record=True)

    out = session.call("gw", "send", [STRIPE, "hello"])
    assert out["result"] is None                     # auto-approved, no ticket
    assert session._owner.prompts["perCall"] == 0
    assert session._auto_consumed == 1
    assert _lines(sink) == [f"send:{STRIPE}:hello"]

    with pytest.raises(ApprovalRequired) as exc:
        session.call("gw", "send", [ATTACKER, "evil"])
    # the ticket the attacker send raised carries the STRUCTURED target (the bound
    # host), and the rule did not cover it (a different host).
    assert exc.value.ticket["classCCapabilities"] == [f'gwsend(host="{ATTACKER}")']
    assert exc.value.ticket["resourceScopes"] == {
        "gwsend": f'gwsend(host="{ATTACKER}")'}
    assert _lines(sink) == [f"send:{STRIPE}:hello"]   # the evil send never fired


@needs_cordis
def test_n1_ledger_records_the_bound_host_not_a_hash(sink, tmp_path):
    """A human yes to the crossing records the BOUND resource valuation (the
    `host=` value that actually crossed), NOT only the opaque argsDigest hash —
    the structured target the distiller and the blast-radius render read."""
    ir = compile_source(_src(sink), "biller.rvl")
    session = _session(operator="alice")
    session.load(copy.deepcopy(ir), record=True)
    _open_wal(session, tmp_path)
    with pytest.raises(ApprovalRequired) as exc:
        session.call("gw", "send", [STRIPE, "hello"])
    approval = session.approve_ticket(exc.value.ticket["hash"])
    assert approval["approved"]
    records = _wal_records(session)
    granted = [r for r in records if r.get("record") == "approval-granted"]
    assert granted, records
    g = granted[-1]
    assert g["resourceScopes"] == {"gwsend": f'gwsend(host="{STRIPE}")'}
    assert g["classCCapabilities"] == [f'gwsend(host="{STRIPE}")']
    assert g["operator"] == "alice"
    # the structured target is recorded, so a rule can name the destination rather
    # than only comparing an opaque args hash.
    assert STRIPE in json.dumps(g)


# ---------------------------------------------------------------------------
# H1: glob-membership GROWTH suspends the rule (fail-closed re-offer)
# ---------------------------------------------------------------------------

@needs_cordis
def test_h1_new_glob_member_suspends_the_rule_and_reprompts(sink):
    """A component ENTERING the `Biller*` glob that was NOT in the reviewed blast
    set is a signature change: the rule suspends and re-offers, so the new member
    is NOT silently auto-approved (open-world glob membership, §6 A1)."""
    ir_invoice = compile_source(_src(sink, "BillerInvoice"), "invoice.rvl")
    ir_refund = compile_source(_src(sink, "BillerRefund"), "refund.rvl")
    session = _session(_rule(component="Biller*"))
    session.load(copy.deepcopy(ir_invoice), record=True)

    # the reviewed member auto-approves before any growth.
    assert session.call("gw", "send", [STRIPE, "a"])["result"] is None
    assert session._owner.prompts["perCall"] == 0
    assert session._auto_reviewed[_rule(component="Biller*").to_dsl()] \
        == frozenset({"BillerInvoice"})

    # swap in a component that ENTERS the glob and was not reviewed.
    session.swap(copy.deepcopy(ir_refund))
    assert session._glob_members("Biller*") == frozenset({"BillerRefund"})

    # the rule is suspended by the membership growth: the new member prompts.
    with pytest.raises(ApprovalRequired):
        session.call("gw", "send", [STRIPE, "b"])
    assert session._auto_rules[0]["suspended"] is True


# ---------------------------------------------------------------------------
# H2: unknown taint at admission is floored to ALL FIVE (fail-closed)
# ---------------------------------------------------------------------------

@needs_cordis
def test_h2_unknown_taint_is_fail_closed_to_all_five(sink):
    """On a tier without a runtime/static taint source (unknown taintOrigins), the
    admission taint gate substitutes ALL FIVE origins, so a rule admitting NO
    taint does not auto-approve — never an empty set waved through."""
    ir = compile_source(_src(sink), "biller.rvl")
    session = _session(_rule())               # admitting = {} (no taint)
    session._runtime_taint_available = False  # a tier with no honest taint source
    session.load(copy.deepcopy(ir), record=True)
    with pytest.raises(ApprovalRequired):
        session.call("gw", "send", [STRIPE, "hi"])
    assert _lines(sink) == []                 # nothing fired, fail-closed


# the tainted fixture feeds the body from an `emission[web]` source, so the send
# scope reaches TWO class-(c) crossings: `gwsend(host=...)` and the `web` capability
# of the source. A rule that covers BOTH isolates the taint subset gate as the only
# variable between the two H2 tests below.
_TAINT_CAPS = (f'gwsend(host="{STRIPE}")', "web")


def _taint_rule(admitting):
    return AutoApproveRule(component="Biller*", caps=_TAINT_CAPS, realm=None,
                           admitting=admitting, uses=10)


@needs_cordis
def test_h2_web_tainted_send_is_not_auto_approved_under_a_no_taint_rule(sink):
    """The realistic H2 case: a send whose body carries web-taint reaches the
    admission gate with `web` in its static over-approximation, so a rule admitting
    NO taint does not cover it (`{web}` is not a subset of `{}`) and it prompts —
    even though the rule's capability cones cover every crossing."""
    ir = compile_source(_src(sink, tainted=True), "tainted.rvl")
    session = _session()
    session.sandbox = Policy(auto_approve_rules=(_taint_rule(frozenset()),))
    session.load(copy.deepcopy(ir), record=True)
    with pytest.raises(ApprovalRequired):
        session.call("gw", "send", [STRIPE, "hi"])
    assert _lines(sink) == []


@needs_cordis
def test_h2_matching_taint_rule_does_auto_approve_the_tainted_send(sink):
    """The dual: the SAME crossing under a rule that DOES admit web-taint
    auto-approves — the subset gate admits exactly the named origins, so taint is
    the only thing that changed between the two tests."""
    ir = compile_source(_src(sink, tainted=True), "tainted.rvl")
    session = _session()
    session.sandbox = Policy(auto_approve_rules=(_taint_rule(frozenset({"web"})),))
    session.load(copy.deepcopy(ir), record=True)
    out = session.call("gw", "send", [STRIPE, "hi"])
    assert out["result"] is None
    assert session._auto_consumed == 1


# ---------------------------------------------------------------------------
# apply / revoke: attributed, WAL-recorded, and the round-trip re-prompts
# ---------------------------------------------------------------------------

def _seed_settled_ledger(session, host=STRIPE, operator="alice"):
    """Six grants over two sessions, one operator, one host — a settled shape the
    distiller offers (the recording a real multi-session ledger would carry)."""
    for i in range(6):
        session._approval_records.append({
            "record": "approval-granted", "component": "Biller",
            "session": f"s{i % 2}", "operator": operator, "realm": "",
            "classCCapabilities": [f'gwsend(host="{host}")'],
            "resourceScopes": {"gwsend": f'gwsend(host="{host}")'}})


@needs_cordis
def test_apply_then_revoke_reprompts_and_is_wal_attributed(sink, tmp_path):
    """A distilled offer applies to a live rule that auto-approves the matching
    crossing; revoking it re-prompts; both carry attributed WAL records."""
    ir = compile_source(_src(sink), "biller.rvl")
    session = _session(operator="alice")
    session.load(copy.deepcopy(ir), record=True)
    _open_wal(session, tmp_path)
    _seed_settled_ledger(session)

    offers = session.distillation_offers()
    assert offers["offers"], offers
    offer_id = offers["offers"][0]["offerId"]
    assert 'gwsend(host="api.stripe.com")' in offers["offers"][0]["rule"]

    applied = session.apply_distillation(offer_id)
    assert applied["applied"] and applied["distilledBy"] == "alice"
    assert applied["reviewedBy"] == "alice"

    # the applied rule auto-approves the matching crossing.
    assert session.call("gw", "send", [STRIPE, "x"])["result"] is None

    revoked = session.revoke_distillation(applied["rule"])
    assert revoked["revoked"] and revoked["count"] == 1

    # the next matching crossing prompts again (fail-closed).
    with pytest.raises(ApprovalRequired):
        session.call("gw", "send", [STRIPE, "y"])

    records = _wal_records(session)
    applied_recs = [r for r in records if r.get("record") == "distillation-applied"]
    revoked_recs = [r for r in records if r.get("record") == "distillation-revoked"]
    assert applied_recs and applied_recs[0]["reviewedBy"] == "alice"
    assert applied_recs[0]["distilledBy"] == "alice"
    assert 'gwsend(host="api.stripe.com")' in applied_recs[0]["rule"]
    assert revoked_recs and revoked_recs[0]["revokedBy"] == "alice"


def test_apply_and_revoke_verbs_gate_under_the_approve_operator_verb():
    """Both `apply_distillation` and `revoke_distillation` map to the item-55
    `approve` verb, so an operator without `approve` is refused (the same gate the
    standing-grant mint and revoke carry)."""
    from revl.mcp import operator as op
    assert op.TOOL_VERB["revl_apply_distillation"] == "approve"
    assert op.TOOL_VERB["revl_revoke_distillation"] == "approve"
    # read-only offers is deliberately ungated (absent from the map).
    assert "revl_distillation_offers" not in op.TOOL_VERB


@needs_cordis
def test_apply_is_refused_for_an_operator_without_approve(sink):
    """The server gate refuses `revl_apply_distillation` for an operator whose
    profile lacks `approve`, mutating nothing (item 55, all-or-nothing)."""
    from revl.mcp import operator as op
    ir = compile_source(_src(sink), "biller.rvl")
    session = _session(operator="mallory")
    # a profile that grants `mallory` swap but NOT approve.
    profile = parse_profile("operator mallory may swap")
    session.operator = profile.get("mallory")
    session.load(copy.deepcopy(ir), record=True)
    _seed_settled_ledger(session, operator="mallory")
    offer_id = session.distillation_offers()["offers"][0]["offerId"]
    decision = op.decide(session, "revl_apply_distillation", {"offerId": offer_id})
    assert decision.gated and not decision.allowed


# ---------------------------------------------------------------------------
# consume-before-fire: a uses-bounded rule cannot double-fire
# ---------------------------------------------------------------------------

@needs_cordis
def test_consume_before_fire_no_double_fire(sink, tmp_path):
    """A rule bounded `uses 1` auto-approves exactly one crossing; the durable
    `approval-consumed` spend is written before the fire, and the second crossing
    prompts (the spend is exhausted — fail-closed, recovery re-prompts)."""
    ir = compile_source(_src(sink), "biller.rvl")
    session = _session(_rule(uses=1))
    session.load(copy.deepcopy(ir), record=True)
    _open_wal(session, tmp_path)

    rid = session._auto_rules[0]["requestId"]
    assert session.call("gw", "send", [STRIPE, "one"])["result"] is None
    assert session._auto_consumed == 1
    assert session._auto_rules[0]["consumed"] is True

    with pytest.raises(ApprovalRequired):        # cannot double-fire the one use
        session.call("gw", "send", [STRIPE, "two"])
    assert _lines(sink) == [f"send:{STRIPE}:one"]

    records = _wal_records(session)
    consumed = [r for r in records if r.get("record") == "approval-consumed"
                and r.get("requestId") == rid]
    assert len(consumed) == 1                     # the durable spend, exactly once


# ---------------------------------------------------------------------------
# byte-identity: a composition with no distilled rule is unchanged
# ---------------------------------------------------------------------------

@needs_cordis
def test_no_distilled_rule_is_byte_identical(sink):
    """With no `auto-approve` rule in the policy, the auto-approve machinery is
    inert: the class-(c) crossing prompts exactly as before, and no rule is
    materialized."""
    ir = compile_source(_src(sink), "biller.rvl")
    session = _session()                          # no auto_approve_rules
    session.load(copy.deepcopy(ir), record=True)
    assert session._auto_rules == []
    with pytest.raises(ApprovalRequired):
        session.call("gw", "send", [STRIPE, "hi"])
    assert session._auto_consumed == 0


@needs_cordis
def test_bare_crossing_ticket_carries_no_resource_scope(sink):
    """A crossing whose method has no registered resource parameter records no
    `resourceScopes` and keeps its bare `classCCapabilities` — the resource
    binding is additive and touches only registered-resource crossings."""
    src = (
        f"extern emission fn ping(msg: Str) = @py {{\n"
        f"    with open({sink!r}, 'a') as _f:\n"
        f"        _f.write(msg)\n"
        f"    return\n"
        f"}}\n"
        "service P { emission fn go(msg: Str) }\n"
        "component Pinger provides p: P {\n"
        "  provide p { fn go(msg) { emit ping(msg) } }\n"
        "}\n"
    )
    ir = compile_source(src, "pinger.rvl")
    session = _session()
    session.load(copy.deepcopy(ir), record=True)
    reach = session._class_map.classify_call("p", "go")
    ticket = session._class_map.build_ticket(reach, ["hi"])
    assert ticket["classCCapabilities"] == ["ping"]
    assert "resourceScopes" not in ticket

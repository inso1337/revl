"""Item 334: a proposal may not inherit a standing authority by taking the NAME
of the component that authority was reviewed over.

Slices 1-4 bound what an agent-authored candidate may REACH (the item-329
untrusted-author profile), what authority ADDRESS it may write for itself
(`no_realm_placement`, G9), what happens when it fails to activate (the health
gate), what live state may cross onto it (the state-compat gate), and what it may
be HANDED (the generation-bound admit handle). This is the other half of the
authority address.

The item-246/251 approval policy scopes a standing auto-approve rule by
`(component glob, realm)`. Slice 2 closed the realm half and said of the glob
half that it "discriminates nothing against" an author who names its own
components. It does not, and that is a fail-open, not a footnote: item 251's H1
bind snapshots the glob's membership as a set of NAMES and suspends the rule when
a name ENTERS it that the operator never reviewed. A name entering is a change
the set can see. A name that stays and means different code is not.

`Gate.propose` replaces the WHOLE live composition with an agent-authored
successor, and under `propose` the successor writes its own component names. So a
candidate that calls itself `BillerInvoice` leaves the rule's membership
byte-identical, is not suspended, and its class-(c) crossings fire under the
operator's distilled rule — with NO prompt and NO ticket. The operator reviewed
some code; the agent replaced it and kept the receipt.

The bind is `_component_signature`: the rule is reviewed against what each member
IS, not only what it is called. It is deliberately author-blind — a rule about the
review, not about `propose` — so it also holds when an OPERATOR rewrites a
reviewed component, and it cannot fail open because a new door forgot to declare
its authorship.

The controls are what make the arms non-vacuous: a REFUSED proposal swaps nothing
and the rule stays live, and a proposal whose component is byte-identical to the
reviewed one stays live too. The refusal is about the code, not about the verb.
"""

from __future__ import annotations

import copy
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl.compiler import compile_source  # noqa: E402
from revl.mcp.approval import ApprovalRequired  # noqa: E402
from revl.policy import AutoApproveRule, Policy  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the propose loop is a runtime property — needs cordis-py "
           "(`sh backends/python/setup.sh`, run under backends/python/.venv)",
)

STRIPE = "api.stripe.com"

# The service interfaces both generations share. `Raw` is the granted seam: the
# trusted provider's host body sits behind it, so the agent candidate reaches a
# class-(c) crossing without declaring an `extern` of its own (which the
# untrusted-author profile refuses outright).
_DECLS = (
    "service Raw { emission[gwsend] fn send(host: Str, body: Str) }\n"
    "service Gw { emission[raw] fn send(host: Str, body: Str) }\n"
)


def _provider(sink: str) -> str:
    """The trusted, operator-supplied provider: one witnessed-free `emission`
    host body behind `raw: Raw`. It is the same bytes in every generation below,
    so it is never the thing that moves."""
    return (
        f"extern emission fn gwsend(host: Str, body: Str) = @py {{\n"
        f"    with open({sink!r}, 'a') as _f:\n"
        f"        _f.write(host + ':' + body + chr(10))\n"
        f"    return\n"
        f"}}\n"
        "component GwSender provides raw: Raw {\n"
        "  provide raw { fn send(h, b) { emit gwsend(h, b) } }\n"
        "}\n"
    )


def _biller(body: str) -> str:
    """A `BillerInvoice` forwarding onto the granted `Raw` seam. `body` is the
    expression it forwards, which is the ONLY thing that differs between the
    operator's reviewed component and the agent's substitute."""
    return (
        "component BillerInvoice requires raw: Raw provides gw: Gw {\n"
        f"  provide gw {{ fn send(h, b) {{ emit raw.send(h, {body}) }} }}\n"
        "}\n"
    )


_REVIEWED = _biller("b")                       # what the operator reviewed
_SUBSTITUTE = _biller('"agent-rewrote:" + b')  # same name, different code


def _base(sink: str) -> str:
    return _DECLS + _REVIEWED + _provider(sink)


def _rule(component: str = "Biller*", uses: int | None = 10) -> AutoApproveRule:
    """The operator's distilled rule. Bare-token caps, so the resource-scope
    `covers` order is not the variable under test: the only thing that can refuse
    a crossing in these arms is the review bind."""
    return AutoApproveRule(component=component, caps=("gwsend", "raw"),
                           realm=None, admitting=frozenset(), uses=uses)


class _Approver:
    """Records every class-(c) ticket the gate raises and answers yes. A silent
    auto-approval raises none, so `len(approver.tickets)` is the measurement
    these arms turn on — not whether the call succeeded."""

    def __init__(self) -> None:
        self.tickets: list[dict] = []

    def __call__(self, ticket: dict) -> bool:
        self.tickets.append(ticket)
        return True


@pytest.fixture
def sink(tmp_path):
    return str(tmp_path / "sink.log")


@pytest.fixture
def gate_factory(tmp_path):
    """Live `Gate`s, each guaranteed closed (the v1 single-gate-per-process
    invariant: a leaked gate soft-bricks every later test)."""
    from revl.gate import Gate
    gates = []

    def _make(rule: AutoApproveRule | None, approver) -> "Gate":
        g = Gate(approval_policy="auto", approver=approver,
                 wal_path=str(tmp_path / f"approval-{len(gates)}.wal"))
        gates.append(g)
        if rule is not None:
            g._session.sandbox = Policy(auto_approve_rules=(rule,))
        return g

    try:
        yield _make
    finally:
        for g in gates:
            g.close()


def _lines(sink: str) -> list[str]:
    return Path(sink).read_text().splitlines() if os.path.exists(sink) else []


# =========================================================================== #
# The arm: a proposal that takes the reviewed NAME does not take the authority.
# =========================================================================== #

@needs_cordis
def test_a_proposal_taking_a_reviewed_name_does_not_inherit_its_auto_approval(
        gate_factory, sink):
    """gen N's `BillerInvoice` is the component the operator's rule was reviewed
    over, and its crossing auto-approves with no prompt. An agent-authored
    successor that REUSES that name leaves the rule's glob membership
    byte-identical — and must still be re-offered, because the code behind the
    name is not the code that was reviewed."""
    approver = _Approver()
    gate = gate_factory(_rule(), approver)
    gate.load(_base(sink))

    # the reviewed component, under the reviewed rule: silent, by design.
    gate.call("gw", "send", [STRIPE, "operator"])
    assert approver.tickets == [], (
        "the reviewed component's crossing should auto-approve — without this "
        "the arm below proves nothing")
    assert gate._session._auto_reviewed[_rule().to_dsl()] \
        == frozenset({"BillerInvoice"})

    # the agent proposes a successor that calls itself by the reviewed name.
    result = gate.propose(_DECLS + _SUBSTITUTE, granted=["Raw"],
                          providers={"provider.rvl": _provider(sink)})
    assert result.admitted and result.swapped, result.message
    # nothing the NAME-set bind can see has changed.
    assert gate._session._glob_members("Biller*") == frozenset({"BillerInvoice"})

    # the substitute's crossing is NOT covered: the rule suspends and re-offers.
    gate.call("gw", "send", [STRIPE, "agent"])
    assert len(approver.tickets) == 1, (
        "the agent-authored successor inherited the operator's distilled "
        "auto-approval by taking the reviewed component's name")
    assert approver.tickets[0]["component"] == "BillerInvoice"
    assert gate._session._auto_rules[0]["suspended"] is True

    # and the effect that fired is the one the operator answered for.
    assert _lines(sink) == [f"{STRIPE}:operator",
                            f"{STRIPE}:agent-rewrote:agent"]


@needs_cordis
def test_the_substituted_crossing_refuses_when_no_operator_answers(
        gate_factory, sink):
    """The re-offer is a real gate, not a notification: with the approver saying
    no, the substitute's crossing does not fire at all and the sink is left
    holding only the operator's own line."""
    from revl.gate import GateRefused

    gate = gate_factory(_rule(), lambda ticket: False)
    gate.load(_base(sink))
    gate.call("gw", "send", [STRIPE, "operator"])     # auto-approved, silent

    result = gate.propose(_DECLS + _SUBSTITUTE, granted=["Raw"],
                          providers={"provider.rvl": _provider(sink)})
    assert result.admitted and result.swapped, result.message
    with pytest.raises(GateRefused):
        gate.call("gw", "send", [STRIPE, "agent"])
    assert _lines(sink) == [f"{STRIPE}:operator"]


# =========================================================================== #
# The controls: the refusal is about the CODE, not about the verb.
# =========================================================================== #

@needs_cordis
def test_a_refused_proposal_leaves_the_reviewed_rule_live(gate_factory, sink):
    """A proposal the decision compile REFUSES swaps nothing, so the reviewed
    component is still the live one and its rule still auto-approves. Without
    this control, an arm above would pass on a gate that suspended the rule at
    the mere sight of a `propose`."""
    approver = _Approver()
    gate = gate_factory(_rule(), approver)
    gate.load(_base(sink))
    gate.call("gw", "send", [STRIPE, "operator"])
    assert approver.tickets == []

    # a candidate that reaches an UNGRANTED service: refused as data (R2).
    refused = gate.propose(
        _DECLS + "service Other { fn k() -> Str }\n"
        + "component BillerInvoice requires raw: Raw requires o: Other"
          " provides gw: Gw {\n"
          "  provide gw { fn send(h, b) { emit raw.send(h, o.k()) } }\n"
          "}\n",
        granted=["Raw"], providers={"provider.rvl": _provider(sink)})
    assert not refused.admitted and not refused.swapped

    gate.call("gw", "send", [STRIPE, "still-operator"])
    assert approver.tickets == [], (
        "a refused proposal changed nothing, so the reviewed rule must still "
        "cover the reviewed component")
    assert gate._session._auto_rules[0]["suspended"] is False


@needs_cordis
def test_a_proposal_of_the_reviewed_code_itself_stays_covered(gate_factory, sink):
    """A candidate whose component is byte-identical to the reviewed one digests
    identically and keeps the rule live — even though it arrived through
    `propose`. The bind is on the code, so identical code is the same review."""
    approver = _Approver()
    gate = gate_factory(_rule(), approver)
    gate.load(_base(sink))
    gate.call("gw", "send", [STRIPE, "operator"])
    assert approver.tickets == []

    result = gate.propose(_DECLS + _REVIEWED, granted=["Raw"],
                          providers={"provider.rvl": _provider(sink)})
    assert result.admitted and result.swapped, result.message

    gate.call("gw", "send", [STRIPE, "same-code"])
    assert approver.tickets == [], (
        "a proposal carrying the REVIEWED component's own code was re-offered — "
        "the bind is over-broad and suspends on the verb rather than the code")
    assert gate._session._auto_rules[0]["suspended"] is False


# =========================================================================== #
# Author-blind: the rule is about the review, not about `propose`.
# =========================================================================== #

@needs_cordis
def test_an_operator_swap_that_rewrites_a_reviewed_component_re_offers(sink):
    """The same bind, reached through a plain `Session.swap` with no agent
    anywhere. A rule reviewed over one body does not carry onto another body
    under the same name, whoever wrote it — so a door that admits untrusted
    source without saying so cannot slip past this."""
    from revl.mcp.session import Session

    session = Session()
    session.approval_policy = "auto"
    session.sandbox = Policy(auto_approve_rules=(_rule(),))
    reviewed = compile_source(_base(sink), "base.rvl")
    rewritten = compile_source(_DECLS + _SUBSTITUTE + _provider(sink),
                               "base.rvl")
    session.load(copy.deepcopy(reviewed), record=True)

    assert session.call("gw", "send", [STRIPE, "operator"])["result"] is None
    assert session._owner.prompts["perCall"] == 0

    # a byte-identical re-materialization is not a change (the control).
    session.swap(copy.deepcopy(reviewed))
    assert session.call("gw", "send", [STRIPE, "again"])["result"] is None
    assert session._owner.prompts["perCall"] == 0

    # rewriting the reviewed component's body, same name, does re-offer.
    session.swap(copy.deepcopy(rewritten))
    with pytest.raises(ApprovalRequired):
        session.call("gw", "send", [STRIPE, "rewritten"])
    assert session._auto_rules[0]["suspended"] is True
    session.unload()

"""Multi-party / quorum approval, Slice 2 - roadmap item 471 (issue #823).

Design: `docs/design/471-quorum-approval.md`, the "Slice 2" section.
`tests/test_471_quorum_approval.py` pins Slice 1: the `require N of {a, b, c}`
clause, the vote protocol, separation of duties, and the durable decision graph.
Slice 1 left two halves of the item design-only, and this suite is both:

  * **the operator verbs.** `escalate_ticket`, `revoke_ticket` and
    `override_ticket` existed with no tool and no verb behind them, so an
    operator on the wire could open a question and vote on it but could never
    hand it up, withdraw it, or break the glass. Here they are driven through
    `revl_escalate`, `revl_revoke` and `revl_override`, and the gating is
    checked as well as the behaviour: the override has its OWN operator verb, so
    an operator who may vote cannot thereby stand in for every vote.
  * **the admission receipt.** One hash-bound artifact carrying the whole
    decision graph for the one crossing the decision authorized, minted where
    the authority is SPENT and re-derivable from the durable rows. The tests that
    matter are the refusals: a receipt whose binding was edited, a receipt
    re-pointed at another candidate (digest recomputed, so the forgery is not
    caught by arithmetic alone), and a receipt for a question the graph does not
    carry are each refused rather than believed.

Every lifecycle path is pinned to its own NAMED outcome - `denied`, `expired`,
`revoked`, `escalated`, and `satisfied` by `votes` or by `override` - because an
outcome an audit cannot name is an outcome it cannot act on.

The harness is `test_471_quorum_approval.py`'s, for the same reason: no cordis
runtime is needed to drive `_approval_decide_call`, the ledger, the votes and the
WAL end to end.
"""

import copy
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import replay as _replay                                       # noqa: E402

from revl.compiler import compile_source                       # noqa: E402
from revl.mcp import operator as op                            # noqa: E402
from revl.mcp import quorum as _quorum_mod                     # noqa: E402
from revl.mcp import server                                    # noqa: E402
from revl.mcp.approval import ApprovalRequired, ClassMap       # noqa: E402
from revl.mcp.approval import _canon, _sha                     # noqa: E402
from revl.mcp.operator import decide, parse_profile            # noqa: E402
from revl.mcp.session import Session, SessionError             # noqa: E402
from revl.policy import ApprovalRule, Policy                   # noqa: E402

_SOURCE = (
    "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('announce:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service Ops {\n"
    "  emission fn shout(sink: Str, msg: Str)\n"
    "}\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn shout(sink, msg) { emit announce(sink, msg) }\n"
    "  }\n"
    "}\n"
)

_THREE = ("alice", "bob", "carol")


def _quorum_rule(require=2, approvers=_THREE, ttl_ms=None):
    return ApprovalRule("announce", ttl_ms, require, tuple(approvers))


def _harness(tmp_path, *, rules=None, token="alice", clock=None, name="wal.json"):
    ir = copy.deepcopy(compile_source(_SOURCE, "quorum471s2.rvl"))
    session = Session()
    session.recorder = _replay.Recorder(copy.deepcopy(ir))
    session._wal_path = str(tmp_path / name)
    session._generation = 1
    session._ensure_wal_open()
    session.approval_policy = "auto"
    session._class_map = ClassMap(ir)
    session.sandbox = Policy(
        approval_rules=tuple(rules if rules is not None else [_quorum_rule()]))
    session.operator = op.Operator(token=token)
    if clock is not None:
        session._clock_ms = lambda: clock["now"]
    return session


def _ticket(session, args=("sink.log", "a")):
    with pytest.raises(ApprovalRequired) as caught:
        session._approval_decide_call("ops", "shout", list(args))
    return caught.value.ticket


def _cross(session, args=("sink.log", "a")):
    """Attempt the crossing. Returns the raise, or None when it went through."""
    try:
        session._approval_decide_call("ops", "shout", list(args))
    except ApprovalRequired as caught:
        return caught
    return None


def _records(session) -> list:
    return _replay.WriteAheadLog.read(session._approval_wal().path)["records"]


def _kinds(session) -> list:
    return [record["record"] for record in _records(session)]


def _admit(session, args=("sink.log", "a")):
    """Take a `require 2 of {alice, bob, carol}` question all the way through:
    two distinct votes from approvers who are not the proposer, then the crossing
    that spends the authority they minted. Returns the ticket."""
    ticket = _ticket(session, args)
    session.approve_ticket(ticket["hash"], as_token="bob")
    session.approve_ticket(ticket["hash"], as_token="carol")
    assert _cross(session, args) is None
    return ticket


@pytest.fixture
def quorum(tmp_path):
    return _harness(tmp_path)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A session the transport verbs act on, so the tools are driven rather than
    the in-process protocol."""
    session = _harness(tmp_path)
    monkeypatch.setattr(server, "SESSION", session)
    return session


# ---------------------------------------------------------------------------
# The exit criterion, re-pinned where Slice 2 can break it
# ---------------------------------------------------------------------------

def test_two_distinct_votes_still_admit_and_one_still_does_not(quorum):
    """The item's exit criterion, restated here because Slice 2 adds three new
    paths into the same decision and a new artifact off the same spend. One vote
    leaves the crossing refused; the second distinct approver's vote admits it."""
    ticket = _ticket(quorum)
    first = quorum.approve_ticket(ticket["hash"], as_token="bob")
    assert (first["approved"], first["counted"]) == (False, 1)
    assert _cross(quorum) is not None
    second = quorum.approve_ticket(ticket["hash"], as_token="carol")
    assert (second["approved"], second["satisfiedBy"]) == (True, "votes")
    assert _cross(quorum) is None


def test_the_proposer_is_still_refused_as_the_sole_approver(quorum):
    """Separation of duties, enforced and not documented: `alice` proposed the
    crossing, so `alice` cannot be one of the two. The refusal is recorded in the
    graph before it is raised, and the receipt carries it."""
    ticket = _ticket(quorum)
    with pytest.raises(SessionError, match="cannot also approve it"):
        quorum.approve_ticket(ticket["hash"], as_token="alice")
    assert "quorum-refused" in _kinds(quorum)
    (refusal,) = [r for r in _records(quorum) if r["record"] == "quorum-refused"]
    assert (refusal["reason"], refusal["voter"]) == ("proposer", "alice")
    assert _cross(quorum) is not None


def test_a_vote_bound_to_one_question_does_not_authorize_another(quorum):
    """The load-bearing property. Two crossings differing only in their arguments
    are two questions with two ticket hashes, and the votes cast against one are
    not authority over the other: the second stays refused, its own decision graph
    is untouched, and the receipt minted for the first names only the first's
    binding."""
    first = _ticket(quorum, ("sink.log", "a"))
    second = _ticket(quorum, ("sink.log", "b"))
    assert first["hash"] != second["hash"]
    assert first["argsDigest"] != second["argsDigest"]

    quorum.approve_ticket(first["hash"], as_token="bob")
    quorum.approve_ticket(first["hash"], as_token="carol")

    assert _cross(quorum, ("sink.log", "b")) is not None, \
        "a vote for one candidate must never authorize a different one"
    assert quorum.quorum_state(second["hash"])["counted"] == 0
    assert _cross(quorum, ("sink.log", "a")) is None

    receipt = quorum.quorum_receipt(first["hash"])
    assert receipt["binding"]["hash"] == first["hash"]
    assert receipt["binding"]["argsDigest"] == first["argsDigest"]
    assert quorum.quorum_receipt(second["hash"]) is None


# ---------------------------------------------------------------------------
# Slice 2a: the operator verbs
# ---------------------------------------------------------------------------

def test_the_escalate_verb_closes_the_vote_path_and_names_its_outcome(wired):
    """`revl_escalate` reaches the protocol, closes the votes, and the outcome is
    the named `escalated`. A later vote is refused rather than counted, and the
    crossing stays refused: escalation only ever narrows authority."""
    ticket = _ticket(wired)
    wired.approve_ticket(ticket["hash"], as_token="bob")

    answer = server._tool_escalate(
        {"hash": ticket["hash"], "reason": "oncall unreachable",
         "asToken": "bob"})
    assert answer["ok"] is True and answer["outcome"] == "escalated"
    assert answer["by"] == "bob" and answer["counted"] == 1
    assert "revl_override" in answer["how_to_resolve"]

    late = server._tool_approve({"hash": ticket["hash"], "asToken": "carol"})
    assert late["ok"] is False
    assert "already escalated" in late["diagnostics"][0]["message"]
    assert _cross(wired) is not None

    (row,) = [r for r in _records(wired) if r["record"] == "quorum-escalated"]
    assert (row["by"], row["reason"]) == ("bob", "oncall unreachable")
    assert row["hash"] == ticket["hash"]
    assert row["candidateHash"] == ticket["candidateHash"]


def test_escalation_refuses_a_bystander_and_records_the_refusal(wired):
    """A name the rule does not carry cannot close somebody else's question, and
    the attempt is a row in the graph rather than an invisible probe."""
    ticket = _ticket(wired)
    answer = server._tool_escalate(
        {"hash": ticket["hash"], "reason": "mine now", "asToken": "mallory"})
    assert answer["ok"] is False
    assert "cannot escalate" in answer["diagnostics"][0]["message"]
    refusals = [r for r in _records(wired) if r["record"] == "quorum-refused"]
    assert [(r["action"], r["reason"], r["voter"]) for r in refusals] == \
        [("escalate", "unknown-approver", "mallory")]
    assert wired.quorum_state(ticket["hash"])["outcome"] is None


def test_the_revoke_verb_withdraws_a_pending_question(wired):
    """`revl_revoke`'s item-471 branch closes a question nobody answered, with the
    named `revoked` outcome. No approval is minted, the crossing stays refused,
    and the row names who closed it and why."""
    ticket = _ticket(wired)
    wired.approve_ticket(ticket["hash"], as_token="bob")

    answer = server._tool_revoke(
        {"hash": ticket["hash"], "reason": "wrong amount", "asToken": "alice"})
    assert answer["ok"] is True and answer["outcome"] == "revoked"
    assert answer["by"] == "alice"
    assert wired._ledger == []
    assert _cross(wired) is not None

    (row,) = [r for r in _records(wired) if r["record"] == "quorum-revoked"]
    assert (row["by"], row["reason"]) == ("alice", "wrong amount")
    assert row["requestId"] == ticket["hash"]


def test_revoke_still_retires_a_standing_grant(tmp_path, monkeypatch):
    """The control for the two-branch tool: a single-party crossing's standing
    grant is still revoked by `capability`, so the item-471 `hash` branch is a
    second object and not a reinterpretation of the first."""
    session = _harness(tmp_path, rules=[ApprovalRule("announce")])
    monkeypatch.setattr(server, "SESSION", session)
    ticket = _ticket(session)
    session.mint_standing_grant(ticket_hash=ticket["hash"], uses=2)
    answer = server._tool_revoke({"capability": "announce"})
    assert answer["ok"] is True and answer["count"] == 1
    assert _cross(session) is not None


def test_the_override_verb_admits_without_the_count_and_says_so(wired):
    """The emergency path. The crossing is admitted with ONE vote against a
    `require 2` rule, and nothing in the record lets that read as a quorum: the
    outcome is `satisfied` by `override`, `counted` is below `require`, and the
    reason and the operator who broke the glass are on the row."""
    ticket = _ticket(wired)
    wired.approve_ticket(ticket["hash"], as_token="bob")

    answer = server._tool_override(
        {"hash": ticket["hash"], "reason": "sev1: refund window closing",
         "asToken": "carol"})
    assert answer["ok"] is True and answer["approved"] is True
    assert answer["satisfiedBy"] == "override"
    assert answer["counted"] == 1 < answer["require"] == 2
    assert _cross(wired) is None

    (row,) = [r for r in _records(wired) if r["record"] == "quorum-override"]
    assert (row["by"], row["reason"]) == ("carol", "sev1: refund window closing")
    assert row["selfOverride"] is False and row["proposer"] == "alice"
    (closed,) = [r for r in _records(wired) if r["record"] == "quorum-satisfied"]
    assert closed["satisfiedBy"] == "override"
    assert closed["counted"] == 1 and closed["require"] == 2
    (granted,) = [r for r in _records(wired) if r["record"] == "approval-granted"]
    assert granted["quorum"]["satisfiedBy"] == "override"
    assert granted["quorum"]["voted"] == ["bob"]


def test_an_override_the_proposer_exercises_is_recorded_as_a_self_override(wired):
    """The override is a different authority, not a vote, so the proposer is not
    refused here the way a self-APPROVAL is. What the record must never do is hide
    it: `selfOverride` is true on the row, so an audit reads one operator closing
    its own question as exactly that."""
    ticket = _ticket(wired)
    answer = server._tool_override(
        {"hash": ticket["hash"], "reason": "sole oncall"})
    assert answer["ok"] is True and answer["satisfiedBy"] == "override"
    (row,) = [r for r in _records(wired) if r["record"] == "quorum-override"]
    assert row["by"] == "alice" and row["selfOverride"] is True
    assert wired.quorum_receipt(ticket["hash"]) is None, \
        "no crossing has spent it yet"
    assert _cross(wired) is None
    receipt = wired.quorum_receipt(ticket["hash"])
    assert receipt["decision"]["override"]["selfOverride"] is True


@pytest.mark.parametrize("reason", [None, "", "   "], ids=["absent", "empty", "blank"])
def test_an_override_with_no_stated_reason_is_refused_and_recorded(wired, reason):
    """An override nobody stated a reason for is an unattributable act, so it is
    refused, and the refusal is a row: an attacker probing the emergency path
    cannot do it invisibly."""
    ticket = _ticket(wired)
    arguments = {"hash": ticket["hash"]}
    if reason is not None:
        arguments["reason"] = reason
    answer = server._tool_override(arguments)
    assert answer["ok"] is False
    assert "must state a reason" in answer["diagnostics"][0]["message"]
    refusals = [r for r in _records(wired) if r["record"] == "quorum-refused"]
    assert [(r["action"], r["reason"]) for r in refusals] == \
        [("override", "no-reason")]
    assert _cross(wired) is not None


def test_an_override_of_a_decided_question_is_refused(wired):
    """An override that could re-open a decided question would be a replay
    primitive for consent: the first one closed it, the second is refused."""
    ticket = _ticket(wired)
    wired.approve_ticket(ticket["hash"], as_token="bob")
    wired.approve_ticket(ticket["hash"], as_token="carol")
    answer = server._tool_override(
        {"hash": ticket["hash"], "reason": "again", "asToken": "bob"})
    assert answer["ok"] is False
    assert "already satisfied" in answer["diagnostics"][0]["message"]
    assert len(wired._ledger) == 1


def test_an_override_of_a_single_party_ticket_is_refused(tmp_path, monkeypatch):
    """There is nothing to override when no quorum binds: approving is the
    ordinary path, and an override with no count to bypass would be an unaudited
    bypass of an ordinary refusal."""
    session = _harness(tmp_path, rules=[ApprovalRule("announce")])
    monkeypatch.setattr(server, "SESSION", session)
    ticket = _ticket(session)
    answer = server._tool_override({"hash": ticket["hash"], "reason": "why not"})
    assert answer["ok"] is False
    assert "does not demand a quorum" in answer["diagnostics"][0]["message"]
    assert session._ledger == []


@pytest.mark.parametrize("tool", ["_tool_escalate", "_tool_override",
                                  "_tool_quorum"], ids=["escalate", "override",
                                                        "quorum"])
def test_a_question_scoped_verb_refuses_an_unknown_ticket(wired, tool):
    """Fail-closed on the outstanding-ticket table, the same way every other
    hash-bound verb is: a hash the server never issued names no question."""
    answer = getattr(server, tool)({"hash": "deadbeef", "reason": "r"})
    assert answer["ok"] is False
    assert "unknown ticket hash" in answer["diagnostics"][0]["message"]


@pytest.mark.parametrize("tool", ["_tool_escalate", "_tool_override",
                                  "_tool_quorum"], ids=["escalate", "override",
                                                        "quorum"])
def test_a_question_scoped_verb_refuses_a_missing_hash(wired, tool):
    """A verb that acts on ONE question and was given none is a usage error
    refused before the session is touched, so no decision graph is opened for a
    call that names nothing."""
    answer = getattr(server, tool)({"reason": "r"})
    assert answer["ok"] is False
    assert "provide `hash`" in answer["diagnostics"][0]["message"]
    assert wired._quorums == {}


# ---------------------------------------------------------------------------
# Slice 2a: the gating, which is the point of giving the override its own verb
# ---------------------------------------------------------------------------

def test_the_override_verb_is_not_the_approve_verb(tmp_path):
    """The authority statement. An operator who may cast one of N votes is NOT
    thereby able to stand in for all of them: `may approve on *` authorizes the
    vote and refuses the override, and `may override on *` is the separate
    address an on-call operator holds to break the glass."""
    session = _harness(tmp_path)
    ticket = _ticket(session)
    session.ir = None

    voter = parse_profile("operator bob may approve on *").get("bob")
    session.operator = voter
    assert decide(session, "revl_approve", {"hash": ticket["hash"]}).allowed
    refused = decide(session, "revl_override",
                     {"hash": ticket["hash"], "reason": "r"})
    assert refused.gated and not refused.allowed

    breaker = parse_profile("operator root may override on *").get("root")
    session.operator = breaker
    assert decide(session, "revl_override",
                  {"hash": ticket["hash"], "reason": "r"}).allowed
    assert not decide(session, "revl_approve", {"hash": ticket["hash"]}).allowed


def test_the_override_verb_scopes_to_the_crossing_component(tmp_path):
    """An override decides ONE question about one candidate, so it scopes to the
    crossing component rather than widening to the whole composition: a
    subject-scoped grant that does not name `Agent` is refused."""
    session = _harness(tmp_path)
    ticket = _ticket(session)
    session.ir = None
    session.operator = parse_profile(
        "operator root may override on Other").get("root")
    refused = decide(session, "revl_override",
                     {"hash": ticket["hash"], "reason": "r"})
    assert refused.gated and not refused.allowed
    session.operator = parse_profile(
        "operator root may override on Agent").get("root")
    assert decide(session, "revl_override",
                  {"hash": ticket["hash"], "reason": "r"}).allowed


def test_escalation_gates_under_approve_and_the_reader_is_ungated():
    """Escalation only narrows authority (the remaining path is the separately
    granted override), so it needs no authority beyond the one to answer the
    question. The decision-graph reader decides nothing and is deliberately
    ungated, recorded as such in the authority-gate suite's own table."""
    assert op.TOOL_VERB["revl_escalate"] == "approve"
    assert op.TOOL_VERB["revl_override"] == "override"
    assert op.TOOL_VERB["revl_override"] != op.TOOL_VERB["revl_approve"]
    assert op.TOOL_VERB["revl_revoke"] == "approve"
    assert "revl_quorum" not in op.TOOL_VERB
    advertised = {tool["name"] for tool in server.TOOLS}
    assert {"revl_escalate", "revl_override", "revl_quorum"} <= advertised


# ---------------------------------------------------------------------------
# Slice 2a: every lifecycle path has its own named outcome
# ---------------------------------------------------------------------------

def _deny(session, ticket):
    """One NO is enough to close this question, and that is the arithmetic
    talking: `require 2 of {alice, bob, carol}` proposed by alice has two
    eligible approvers, so the moment one of them says no the count cannot be
    reached by anybody left."""
    session.approve_ticket(ticket["hash"], vote="deny", as_token="bob")


def test_denial_closes_the_question_as_denied_and_names_the_deniers(quorum):
    """A NO is a recorded vote, and the question closes when the count is no
    longer reachable. The row distinguishes a human refusal (`denied`) from the
    arithmetic running out (`unreachable`), so an audit does not blame an approver
    for a policy's count."""
    ticket = _ticket(quorum)
    _deny(quorum, ticket)
    state = quorum.quorum_state(ticket["hash"])
    assert state["outcome"] == "denied" and state["counted"] == 0
    (row,) = [r for r in _records(quorum) if r["record"] == "quorum-denied"]
    assert row["reason"] == "denied" and row["denied"] == ["bob"]
    assert _cross(quorum) is not None
    assert quorum._ledger == []


def test_a_denial_that_leaves_the_count_reachable_leaves_it_open(tmp_path):
    """The other side of the line: a NO only closes the question once the count is
    arithmetically dead. With four named approvers and the proposer outside the
    set, one no leaves two eligible approvers and `require 2` is still reachable,
    so the question stays open for them."""
    session = _harness(
        tmp_path, token="dave",
        rules=[_quorum_rule(2, ("alice", "bob", "carol", "erin"))])
    ticket = _ticket(session)
    session.approve_ticket(ticket["hash"], vote="deny", as_token="alice")
    assert session.quorum_state(ticket["hash"])["outcome"] is None
    session.approve_ticket(ticket["hash"], as_token="bob")
    session.approve_ticket(ticket["hash"], as_token="carol")
    assert _cross(session) is None
    receipt = session.quorum_receipt(ticket["hash"])
    assert [(r["voter"], r["vote"]) for r in receipt["decision"]["votes"]] == \
        [("alice", "deny"), ("bob", "approve"), ("carol", "approve")]
    assert receipt["decision"]["counted"] == 2


def test_timeout_closes_the_question_as_expired(tmp_path):
    """The deadline is the ticket's own ttl and the timeout is irreversible (the
    item-427 dead latch): the first vote past it latches `expiredAt`, writes
    `quorum-expired` with the count it had, and no later vote can be counted."""
    clock = {"now": 1_000}
    session = _harness(tmp_path, rules=[_quorum_rule(ttl_ms=30_000)], clock=clock)
    ticket = _ticket(session)
    session.approve_ticket(ticket["hash"], as_token="bob")
    clock["now"] += 60_000
    with pytest.raises(SessionError, match="lapsed"):
        session.approve_ticket(ticket["hash"], as_token="carol")
    (row,) = [r for r in _records(session) if r["record"] == "quorum-expired"]
    assert row["counted"] == 1 and row["expiresAt"] == 31_000
    assert session.quorum_state(ticket["hash"])["outcome"] == "expired"
    assert session._ledger == []


@pytest.mark.parametrize("outcome,kind", [
    ("denied", "quorum-denied"),
    ("expired", "quorum-expired"),
    ("escalated", "quorum-escalated"),
    ("revoked", "quorum-revoked"),
    ("satisfied", "quorum-satisfied"),
], ids=["deny", "timeout", "escalate", "revoke", "override"])
def test_every_lifecycle_path_has_its_own_named_outcome(tmp_path, outcome, kind):
    """The item asks for denial, timeout, revoke, escalation and emergency
    override each with an explicit named outcome. One table, five paths, five
    names, each with its own durable record kind - no path ends in an unnamed
    state, and none of them shares a name with another."""
    clock = {"now": 1_000}
    session = _harness(tmp_path, rules=[_quorum_rule(ttl_ms=30_000)], clock=clock)
    ticket = _ticket(session)
    if outcome == "denied":
        _deny(session, ticket)
    elif outcome == "expired":
        clock["now"] += 60_000
        with pytest.raises(SessionError):
            session.approve_ticket(ticket["hash"], as_token="bob")
    elif outcome == "escalated":
        session.escalate_ticket(ticket["hash"], reason="up", as_token="bob")
    elif outcome == "revoked":
        session.revoke_ticket(ticket["hash"], reason="back", as_token="alice")
    else:
        session.override_ticket(ticket["hash"], reason="sev1", as_token="bob")
    state = session.quorum_state(ticket["hash"])
    assert state["outcome"] == outcome
    assert kind in _kinds(session)
    if outcome == "satisfied":
        assert state["satisfiedBy"] == "override"
    else:
        assert state["satisfiedBy"] is None


# ---------------------------------------------------------------------------
# Slice 2b: the admission receipt
# ---------------------------------------------------------------------------

def test_the_receipt_is_minted_at_admission_and_not_at_the_decision(quorum):
    """A satisfied quorum that no crossing ever spent authorized nothing, and a
    receipt naming a fire that never happened would be a false record. So the
    receipt is minted where the authority is SPENT - after the durable
    `approval-consumed`, before the fire."""
    ticket = _ticket(quorum)
    quorum.approve_ticket(ticket["hash"], as_token="bob")
    quorum.approve_ticket(ticket["hash"], as_token="carol")
    assert quorum.quorum_state(ticket["hash"])["outcome"] == "satisfied"
    assert quorum.quorum_receipt(ticket["hash"]) is None

    assert _cross(quorum) is None
    receipt = quorum.quorum_receipt(ticket["hash"])
    assert receipt is not None
    assert receipt["spend"]["requestId"] == ticket["hash"]
    assert "approval-consumed" in _kinds(quorum)


def test_the_receipt_carries_the_whole_decision_graph(quorum):
    """The item's "full decision graph in the receipt". Every axis: the binding
    (ticket hash, candidate hash, component, round, capabilities, expiry, the
    arguments digest and the session), the rule as written with its count and its
    named approver set, the proposer, every counted vote, and every cast that was
    REFUSED with the reason - because "two approvers said yes" reads differently
    when one was turned away first."""
    ticket = _ticket(quorum)
    with pytest.raises(SessionError):
        quorum.approve_ticket(ticket["hash"], as_token="mallory")
    with pytest.raises(SessionError):
        quorum.approve_ticket(ticket["hash"], as_token="alice")
    quorum.approve_ticket(ticket["hash"], as_token="bob")
    quorum.approve_ticket(ticket["hash"], as_token="carol")
    assert _cross(quorum) is None

    receipt = quorum.quorum_receipt(ticket["hash"])
    assert receipt["kind"] == _quorum_mod.RECEIPT_KIND
    assert receipt["version"] == _quorum_mod.RECEIPT_VERSION
    binding = receipt["binding"]
    assert binding["hash"] == ticket["hash"]
    assert binding["candidateHash"] == ticket["candidateHash"]
    assert binding["component"] == "Agent" and binding["round"] == 1
    assert binding["capabilities"] == ["announce"]
    assert binding["session"] == quorum._session_id
    assert receipt["rule"] == {
        "text": "require 2 of {alice, bob, carol}", "require": 2,
        "approvers": list(_THREE), "proposer": "alice"}
    decision = receipt["decision"]
    assert decision["outcome"] == "satisfied"
    assert decision["satisfiedBy"] == "votes" and decision["counted"] == 2
    assert [(r["voter"], r["vote"]) for r in decision["votes"]] == \
        [("bob", "approve"), ("carol", "approve")]
    assert [(r["voter"], r["reason"]) for r in decision["refusals"]] == \
        [("mallory", "unknown-approver"), ("alice", "proposer")]


def test_the_receipt_verifies_against_the_durable_decision_graph(quorum):
    """The receipt is a JOIN over the WAL rows, not a second copy of them, so it
    is re-derivable: `verify` rebuilds it from the graph and the ledger entry and
    agrees. That is what makes it safe to hand out."""
    ticket = _admit(quorum)
    receipt = quorum.quorum_receipt(ticket["hash"])
    assert quorum.verify_quorum_receipt(receipt) == {"ok": True, "reasons": []}
    # the rows it was derived from are the durable ones, not a private mirror
    durable = [r["record"] for r in _records(quorum)]
    assert durable == ["quorum-open", "quorum-vote", "quorum-vote",
                       "quorum-satisfied", "approval-granted",
                       "approval-consumed"]


def test_an_edited_receipt_does_not_hash_to_its_own_body(quorum):
    """The cheap forgery: change a field and keep the digest. Refused on the
    digest, before any comparison with the graph."""
    ticket = _admit(quorum)
    forged = copy.deepcopy(quorum.quorum_receipt(ticket["hash"]))
    forged["decision"]["counted"] = 3
    verdict = quorum.verify_quorum_receipt(forged)
    assert verdict["ok"] is False
    assert any(r.startswith("digest") for r in verdict["reasons"])


def test_a_receipt_re_pointed_at_another_candidate_is_refused(quorum):
    """The forgery that arithmetic alone does not catch: edit the binding AND
    recompute the digest, so the receipt is internally consistent and asserts a
    different candidate. It is refused because the verifier re-derives the body
    from the decision graph, which carries the real candidate hash - a receipt
    cannot assert a binding the record does not hold."""
    ticket = _admit(quorum)
    forged = copy.deepcopy(quorum.quorum_receipt(ticket["hash"]))
    forged["binding"]["candidateHash"] = "0" * 64
    forged.pop("digest")
    forged["digest"] = _sha(_canon(forged))

    verdict = quorum.verify_quorum_receipt(forged)
    assert verdict["ok"] is False
    assert not any(r.startswith("digest") for r in verdict["reasons"])
    assert any(r.startswith("binding") for r in verdict["reasons"])


def test_a_receipt_with_padded_votes_is_refused(quorum):
    """The forgery that matters most: a receipt claiming a vote nobody cast. The
    graph has two rows and the receipt would claim three, so the rebuilt decision
    disagrees and the receipt is refused."""
    ticket = _admit(quorum)
    forged = copy.deepcopy(quorum.quorum_receipt(ticket["hash"]))
    forged["decision"]["votes"].append(
        {"voteId": f"{ticket['hash']}#v3", "voter": "alice",
         "vote": "approve", "at": 1, "round": 1})
    forged["decision"]["counted"] = 3
    forged.pop("digest")
    forged["digest"] = _sha(_canon(forged))
    verdict = quorum.verify_quorum_receipt(forged)
    assert verdict["ok"] is False
    assert any(r.startswith("decision") for r in verdict["reasons"])


def test_a_receipt_for_a_question_nobody_has_is_refused(quorum):
    """A receipt naming a decision this session's graph does not carry is refused,
    never assumed: an unverifiable receipt is not a verified one."""
    ticket = _admit(quorum)
    forged = copy.deepcopy(quorum.quorum_receipt(ticket["hash"]))
    forged["binding"]["requestId"] = "no-such-question"
    forged.pop("digest")
    forged["digest"] = _sha(_canon(forged))
    verdict = quorum.verify_quorum_receipt(forged)
    assert verdict["ok"] is False
    assert any(r.startswith("no-decision") for r in verdict["reasons"])


@pytest.mark.parametrize("mutate,reason", [
    (lambda r: r.update(kind="revl.deploy.receipt"), "unknown-kind"),
    (lambda r: r.update(version=99), "unknown-version"),
    (lambda r: r.pop("spend"), "no-spend"),
], ids=["kind", "version", "no-spend"])
def test_a_document_that_is_not_an_admission_receipt_is_refused(quorum, mutate,
                                                               reason):
    """Fail-closed on the shape before the content: another protocol's document,
    a shape from a version this verifier does not read, and a receipt naming no
    spend are each refused by name rather than re-derived."""
    ticket = _admit(quorum)
    forged = copy.deepcopy(quorum.quorum_receipt(ticket["hash"]))
    mutate(forged)
    verdict = quorum.verify_quorum_receipt(forged)
    assert verdict["ok"] is False
    assert any(r.startswith(reason) for r in verdict["reasons"])


def test_the_override_receipt_says_override_and_carries_the_reason(wired):
    """An override's receipt is legible as an override: `satisfiedBy` is
    `override`, the counted votes are below `require`, and the reason and the
    operator who broke the glass ride with it. Nothing in the artifact lets a
    later reader mistake it for the quorum it stood in for."""
    ticket = _ticket(wired)
    wired.approve_ticket(ticket["hash"], as_token="bob")
    server._tool_override(
        {"hash": ticket["hash"], "reason": "sev1", "asToken": "carol"})
    assert _cross(wired) is None

    receipt = wired.quorum_receipt(ticket["hash"])
    assert receipt["decision"]["satisfiedBy"] == "override"
    assert receipt["decision"]["counted"] == 1
    assert receipt["rule"]["require"] == 2
    assert receipt["decision"]["override"]["by"] == "carol"
    assert receipt["decision"]["override"]["reason"] == "sev1"
    assert wired.verify_quorum_receipt(receipt)["ok"] is True


def test_a_single_party_approval_mints_no_quorum_receipt(tmp_path):
    """The clause is inert when it is not written. A rule naming no approvers and
    demanding one keeps the byte-identical item-246 shape: no decision graph, no
    receipt, and the crossing admits on one yes."""
    session = _harness(tmp_path, rules=[ApprovalRule("announce")])
    ticket = _ticket(session)
    session.approve_ticket(ticket["hash"])
    assert _cross(session) is None
    assert session._quorums == {} and session._quorum_receipts == {}
    assert session.quorum_receipt(ticket["hash"]) is None
    assert "quorum-open" not in _kinds(session)


def test_the_receipt_is_not_re_dated_by_a_second_read(quorum):
    """Consume-before-fire spends an entry exactly once, so the receipt is minted
    once. A second read returns the same artifact rather than a re-dated one -
    a receipt whose `consumedAt` moved would be a second admission on one yes."""
    ticket = _admit(quorum)
    first = quorum.quorum_receipt(ticket["hash"])
    assert quorum._mint_admission_receipt(
        next(e for e in quorum._ledger
             if e["requestId"] == ticket["hash"])) is first
    assert quorum.quorum_receipt(ticket["hash"]) == first


# ---------------------------------------------------------------------------
# Slice 2b: the reader verb
# ---------------------------------------------------------------------------

def test_the_quorum_verb_reports_the_graph_and_the_verified_receipt(wired):
    """`revl_quorum` is the "auditable after the fact" surface: the whole graph,
    the receipt once the authority was spent, and - on request - the verdict of
    re-deriving that receipt from the durable rows."""
    ticket = _ticket(wired)
    with pytest.raises(SessionError):
        wired.approve_ticket(ticket["hash"], as_token="alice")
    wired.approve_ticket(ticket["hash"], as_token="bob")

    open_report = server._tool_quorum({"hash": ticket["hash"]})
    assert open_report["ok"] is True and open_report["outcome"] is None
    assert open_report["counted"] == 1 and open_report["require"] == 2
    assert open_report["outstanding"] == ["alice", "carol"]
    assert [r["reason"] for r in open_report["refusals"]] == ["proposer"]
    assert "receipt" not in open_report

    wired.approve_ticket(ticket["hash"], as_token="carol")
    assert _cross(wired) is None
    final = server._tool_quorum({"hash": ticket["hash"], "verify": True})
    assert final["outcome"] == "satisfied" and final["satisfiedBy"] == "votes"
    assert final["receipt"]["binding"]["hash"] == ticket["hash"]
    assert final["verification"] == {"ok": True, "reasons": []}


def test_the_quorum_verb_reports_a_single_party_ticket_as_no_quorum(tmp_path,
                                                                   monkeypatch):
    """A ticket that demands no quorum reports that rather than inventing an empty
    graph, so a caller cannot read "no votes" as "votes pending"."""
    session = _harness(tmp_path, rules=[ApprovalRule("announce")])
    monkeypatch.setattr(server, "SESSION", session)
    ticket = _ticket(session)
    report = server._tool_quorum({"hash": ticket["hash"]})
    assert report["ok"] is True and report["quorum"] is False
    assert "no approver set" in report["detail"]


def test_the_slice2_verbs_are_documented_where_docgen_checks(quorum):
    """The doc surface is part of shipping a verb: `docs/mcp-reference.md` gets a
    section per tool and `docs/guide-ai-agents.md` names every one, both checked
    by `tools/docgen.py`. This pins the operator-facing half docgen does not:
    the new verbs are named in the operator-capability doc that says which
    authority each one needs."""
    caps = (_ROOT / "docs" / "operator-capabilities.md").read_text(
        encoding="utf-8")
    assert "override" in caps and "revl_override" in caps
    assert "revl_escalate" in caps


# ---------------------------------------------------------------------------
# A decision, once made, is not unmade by an act that comes after it
#
# The question's deadline and the ledger entry's expiry are both the ticket's
# ttl measured from DIFFERENT instants: the question from ticket creation
# (`_open_quorum`), the entry from the DECISION (`_mint_ticket_entry`). An entry
# minted from votes cast late therefore OUTLIVES the question that authorized it,
# and inside that window an act arriving after the decision used to rewrite the
# decision. Two directions, one root: an act after admission changed the answer
# about an admission.
# ---------------------------------------------------------------------------

_LEASE_SOURCE = (
    'extern emission[fs.write(path="/tmp")] fn wr(sink: Str, msg: Str)'
    " = @py {\n"
    "    with open(sink, 'a') as f: f.write('w:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service Ops { emission fn go(sink: Str, msg: Str) }\n"
    "component Agent provides ops: Ops {\n"
    '  let l = effect lease fs.write(path="/tmp") ttl 10m uses 3 '
    "undo l.revoke()\n"
    "  provide ops { fn go(sink, msg) { emit wr(sink, msg) } }\n"
    "}\n"
)

_LEASE_CAP = 'fs.write(path="/tmp")'


def _lease_harness(tmp_path, clock, *, ttl_ms=1_000, name="lease.json"):
    """A session whose only crossing is guarded by a `lease` effect, so the
    decision's authority is spent through the item-61 lease gate - the path on
    which a rewritten decision shows up as a refused load rather than a bad
    record."""
    ir = copy.deepcopy(compile_source(_LEASE_SOURCE, "quorum471lease.rvl"))
    session = Session()
    session.recorder = _replay.Recorder(copy.deepcopy(ir))
    session._wal_path = str(tmp_path / name)
    session._generation = 1
    session._ensure_wal_open()
    session.approval_policy = "auto"
    session._class_map = ClassMap(ir)
    session.sandbox = Policy(approval_rules=(
        ApprovalRule(_LEASE_CAP, ttl_ms, 2, tuple(_THREE)),))
    session.operator = op.Operator(token="alice")
    session._clock_ms = lambda: clock["now"]
    return session, ir


def _lease_gate(session, ir):
    """Attempt the load. Returns the ApprovalRequired, or None when the lease
    gate let it through."""
    try:
        session._enforce_lease_gate(ir)
    except ApprovalRequired as caught:
        return caught
    return None


def test_a_late_vote_does_not_rewrite_a_decided_question(tmp_path):
    """The question's deadline bounds its VOTES, not its answer. A vote arriving
    after the decision was made is refused, and the decision it arrived too late
    for is left exactly as it was: `satisfied` stays `satisfied` and no
    `quorum-expired` row is written. A `quorum-expired` row here would be the
    record of a timeout that never happened."""
    clock = {"now": 1_000}
    session = _harness(tmp_path, rules=[_quorum_rule(ttl_ms=1_000)],
                       clock=clock)
    ticket = _ticket(session)
    session.approve_ticket(ticket["hash"], as_token="bob")
    session.approve_ticket(ticket["hash"], as_token="carol")
    assert session.quorum_state(ticket["hash"])["outcome"] == "satisfied"

    clock["now"] += 5_000
    with pytest.raises(SessionError, match="already satisfied"):
        session.approve_ticket(ticket["hash"], vote="approve", as_token="bob")

    state = session.quorum_state(ticket["hash"])
    assert state["outcome"] == "satisfied"
    assert state["satisfiedBy"] == "votes"
    assert "quorum-expired" not in _kinds(session)
    assert state["refusals"][-1]["reason"] == "closed-decision"


@pytest.mark.parametrize("outcome,match", [
    ("denied", "already denied"),
    ("escalated", "already escalated"),
    ("revoked", "already revoked"),
], ids=["deny", "escalate", "revoke"])
def test_a_late_vote_never_rewrites_any_closed_outcome(tmp_path, outcome, match):
    """Every named outcome, not just `satisfied`: the rewrite was unconditional,
    so a denial, an escalation and a revocation were all equally overwritable by
    one late vote from any operator holding `approve`. None of them is now."""
    clock = {"now": 1_000}
    session = _harness(tmp_path, rules=[_quorum_rule(ttl_ms=1_000)], clock=clock)
    ticket = _ticket(session)
    if outcome == "denied":
        _deny(session, ticket)
    elif outcome == "escalated":
        session.escalate_ticket(ticket["hash"], reason="up", as_token="bob")
    else:
        session.revoke_ticket(ticket["hash"], reason="back", as_token="alice")

    clock["now"] += 5_000
    with pytest.raises(SessionError, match=match):
        session.approve_ticket(ticket["hash"], vote="approve", as_token="bob")

    assert session.quorum_state(ticket["hash"])["outcome"] == outcome
    assert "quorum-expired" not in _kinds(session)


def test_an_expired_question_still_reads_as_lapsed(tmp_path):
    """The other direction of the same ordering: a question that genuinely ran
    out of time is still refused as `lapsed`, because `expired` is the one
    outcome the lapse cannot change. Reordering the checks must not cost an audit
    the name of the timeout."""
    clock = {"now": 1_000}
    session = _harness(tmp_path, rules=[_quorum_rule(ttl_ms=1_000)], clock=clock)
    ticket = _ticket(session)
    clock["now"] += 5_000
    with pytest.raises(SessionError, match="lapsed"):
        session.approve_ticket(ticket["hash"], vote="approve", as_token="bob")
    assert session.quorum_state(ticket["hash"])["outcome"] == "expired"


def test_a_late_vote_does_not_wedge_the_lease_the_decision_authorized(tmp_path):
    """The reachable window, and the reason this is a live defect rather than a
    bookkeeping one. The question opens at t=1000 with `expiresAt` 2000; the votes
    land at t=1500, so the ledger entry they mint is dated from the DECISION and
    expires at 2500. At t=2200 the question has lapsed and the authority has not.
    A late vote there used to lapse the satisfied question, and the lease gate -
    which asks the decision graph whether the load is authorized - then refused a
    load whose authority was still live and spendable. The ticket re-raised with
    the SAME hash, so no fresh question could clear it: the decision was wedged."""
    clock = {"now": 1_000}
    session, ir = _lease_harness(tmp_path, clock)

    first = _lease_gate(session, ir)
    assert first is not None, "the load must raise the lease ticket"
    ticket = first.ticket
    assert session._quorums[ticket["hash"]]["expiresAt"] == 2_000

    clock["now"] = 1_500
    session.approve_ticket(ticket["hash"], as_token="bob")
    session.approve_ticket(ticket["hash"], as_token="carol")
    (entry,) = session._ledger
    assert session._quorums[ticket["hash"]]["outcome"] == "satisfied"
    assert entry["expiresAt"] == 2_500 > session._quorums[ticket["hash"]]["expiresAt"]

    clock["now"] = 2_200
    with pytest.raises(SessionError, match="already satisfied"):
        session.approve_ticket(ticket["hash"], vote="approve", as_token="bob")
    assert session.quorum_state(ticket["hash"])["outcome"] == "satisfied"

    assert _lease_gate(session, ir) is None, (
        "the load the satisfied decision authorized must still be admitted")
    assert [grant["lease"] for grant in session._grants] == [True]
    assert [e["consumed"] for e in session._ledger] == [True]


def test_a_refusal_after_the_spend_does_not_invalidate_the_receipt(quorum):
    """The other direction: a receipt must not be un-verified by a later probe.
    A genuine, correctly spent admission re-verified as forged - "the receipt
    disagrees with the decision graph" - the moment anybody probed the question
    afterwards, because the receipt's refusal set was rebuilt from the LIVE rows.
    A refusal written after the spend is a fact about the question's afterlife,
    not about the decision that admitted, so it stays on the graph and in the
    reader but out of the receipt."""
    ticket = _admit(quorum)
    receipt = quorum.quorum_receipt(ticket["hash"])
    assert quorum.verify_quorum_receipt(receipt) == {"ok": True, "reasons": []}

    with pytest.raises(SessionError, match="already satisfied"):
        quorum.approve_ticket(ticket["hash"], vote="approve", as_token="bob")

    assert quorum.verify_quorum_receipt(receipt) == {"ok": True, "reasons": []}
    assert quorum.quorum_receipt(ticket["hash"]) == receipt, (
        "the same artifact, not a re-dated one")
    # the probe is not hidden: it is on the graph and the live reader shows it
    assert "quorum-refused" in _kinds(quorum)
    assert [r["reason"] for r in quorum.quorum_state(ticket["hash"])["refusals"]] \
        == ["closed-decision"]
    assert receipt["decision"]["refusals"] == []


def test_a_refusal_before_the_spend_is_still_in_the_receipt(quorum):
    """The bound is the spend's own clock reading, not "drop the refusals": a cast
    turned away while the question was still open is part of the decision and must
    ride with the receipt, or the receipt would stop being a full decision graph."""
    ticket = _ticket(quorum)
    with pytest.raises(SessionError):
        quorum.approve_ticket(ticket["hash"], as_token="mallory")
    quorum.approve_ticket(ticket["hash"], as_token="bob")
    quorum.approve_ticket(ticket["hash"], as_token="carol")
    assert _cross(quorum) is None

    with pytest.raises(SessionError, match="already satisfied"):
        quorum.approve_ticket(ticket["hash"], vote="approve", as_token="bob")

    receipt = quorum.quorum_receipt(ticket["hash"])
    assert [(r["voter"], r["reason"]) for r in receipt["decision"]["refusals"]] \
        == [("mallory", "unknown-approver")]
    assert quorum.verify_quorum_receipt(receipt)["ok"] is True


def test_the_refusal_bound_is_re_derived_and_not_stored(quorum):
    """The receipt carries no second copy of the bound: the verifier re-derives it
    from the durable rows and the entry the receipt names - the position of the
    `approval-granted` row that minted the spent authority. So a forger cannot
    widen the refusal set at all: an extra row in the body is a row the graph does
    not carry, and the only way to make it agree is to edit the body, which breaks
    the digest."""
    ticket = _admit(quorum)
    receipt = quorum.quorum_receipt(ticket["hash"])
    with pytest.raises(SessionError, match="already satisfied"):
        quorum.approve_ticket(ticket["hash"], vote="approve", as_token="bob")

    forged = copy.deepcopy(receipt)
    forged["decision"]["refusals"].append(
        {"action": "vote", "reason": "closed-decision", "voter": "bob",
         "counted": 2})
    forged.pop("digest")
    forged["digest"] = _sha(_canon(forged))
    verdict = quorum.verify_quorum_receipt(forged)
    assert verdict["ok"] is False
    assert any(r.startswith("decision") for r in verdict["reasons"])


# --- the fix must not widen anything -------------------------------------

def test_an_override_of_an_escalated_question_is_still_lapsed(tmp_path):
    """The one place the ordering could have relaxed a refusal. `override_ticket`
    deliberately carves escalation out of the closed check - after escalation the
    override is the only path left - so if that carve-out ran before the lapse, an
    escalated question past its deadline would have become overridable where it
    was refused before. It is refused exactly as before."""
    clock = {"now": 1_000}
    session = _harness(tmp_path, rules=[_quorum_rule(ttl_ms=1_000)], clock=clock)
    ticket = _ticket(session)
    session.approve_ticket(ticket["hash"], as_token="bob")
    session.escalate_ticket(ticket["hash"], reason="stalled", as_token="alice")
    assert session.quorum_state(ticket["hash"])["outcome"] == "escalated"

    clock["now"] += 5_000
    with pytest.raises(SessionError, match="lapsed"):
        session.override_ticket(ticket["hash"], reason="sev1", as_token="carol")
    assert session.quorum_state(ticket["hash"])["outcome"] == "escalated"


@pytest.mark.parametrize("verb,match", [
    ("override", "already satisfied"),
    ("escalate", "already satisfied"),
    ("revoke", "already satisfied"),
], ids=["override", "escalate", "revoke"])
def test_no_verb_rewrites_a_satisfied_question_past_its_deadline(
        tmp_path, verb, match):
    """Every operator verb that consults the deadline, not just the vote path:
    each still refuses a satisfied question past its deadline, and each refuses by
    the decision's own name rather than rewriting it to a timeout."""
    clock = {"now": 1_000}
    session = _harness(tmp_path, rules=[_quorum_rule(ttl_ms=1_000)], clock=clock)
    ticket = _ticket(session)
    session.approve_ticket(ticket["hash"], as_token="bob")
    session.approve_ticket(ticket["hash"], as_token="carol")

    clock["now"] += 5_000
    with pytest.raises(SessionError, match=match):
        if verb == "override":
            session.override_ticket(ticket["hash"], reason="sev1", as_token="carol")
        elif verb == "escalate":
            session.escalate_ticket(ticket["hash"], reason="up", as_token="bob")
        else:
            session.revoke_ticket(ticket["hash"], reason="back", as_token="alice")

    assert session.quorum_state(ticket["hash"])["outcome"] == "satisfied"
    assert "quorum-expired" not in _kinds(session)

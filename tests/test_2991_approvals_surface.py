"""The approval preview and query surface, and the E-Stop that revokes pending
questions (issue #2991).

Design: the live session's approval machinery of items 246/344/471 plus the
E-Stop of item 443. The issue's own tests:

  * a golden `list` and `show` against a fixture session;
  * E-Stop mid-pending turns the ticket `revoked`, a replay is refused with the
    distinct refusal class, and an expired question stays distinct from a
    revoked one in the recover vocabulary.

The harness is `test_471_quorum_slice2.py`'s: no cordis runtime is needed to
issue tickets, answer them, halt the session (a stub driver supplies the runtime
call) or read the approval WAL.
"""

import copy
import hashlib
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import replay as _replay                                       # noqa: E402

from revl import recovery                                      # noqa: E402
from revl.compiler import compile_source                       # noqa: E402
from revl.mcp import approvals_surface                          # noqa: E402
from revl.mcp import operator as op                             # noqa: E402
from revl.mcp import server                                    # noqa: E402
from revl.mcp.approval import ApprovalRequired, ClassMap       # noqa: E402
from revl.mcp.session import ApprovalRefused, Session, SessionError  # noqa: E402
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

_SECRETS = {name: f"{name}-secret-for-tests-2991"
            for name in ("alice", "bob", "carol")}

_PROFILE = "".join(
    f"operator {name} key sha256:"
    f"{hashlib.sha256(secret.encode()).hexdigest()}\n"
    f"operator {name} may approve, override on *\n"
    for name, secret in sorted(_SECRETS.items()))


def _harness(tmp_path, *, rules=None, clock=None):
    ir = copy.deepcopy(compile_source(_SOURCE, "approvals2991.rvl"))
    session = Session()
    session.recorder = _replay.Recorder(copy.deepcopy(ir))
    session._wal_path = str(tmp_path / "wal.json")
    session._generation = 1
    session._ensure_wal_open()
    session.approval_policy = "auto"
    session._class_map = ClassMap(ir)
    session.sandbox = Policy(
        approval_rules=tuple(rules) if rules is not None else ())
    session.operator_registry = op.parse_profile(_PROFILE)
    session.operator = session.operator_registry.get("alice")
    if clock is not None:
        session._clock_ms = lambda: clock["now"]
    return session


class _FakeRuntime:
    def __init__(self):
        self.state = None

    def estop(self, reason, operator=None):
        self.state = {"halted": True, "reason": reason, "operator": operator,
                      "residue": []}
        return dict(self.state)

    def estop_state(self):
        return self.state

    def estop_residue(self):
        return []


class _FakeDriver:
    def __init__(self):
        self.runtime = _FakeRuntime()


def _halt(session, *, reason="operator halt", operator="alice") -> dict:
    session._driver = _FakeDriver()
    try:
        return session.estop(reason, operator=operator)
    finally:
        session._driver = None


def _ticket(session, args=("sink.log", "a")) -> dict:
    with pytest.raises(ApprovalRequired) as caught:
        session._approval_decide_call("ops", "shout", list(args))
    return caught.value.ticket


def _cross(session, args=("sink.log", "a")):
    try:
        session._approval_decide_call("ops", "shout", list(args))
    except ApprovalRequired as caught:
        return caught
    return None


def _records(session) -> list:
    return _replay.WriteAheadLog.read(session._approval_wal().path)["records"]


def _kinds(session) -> list:
    return [record["record"] for record in _records(session)]


def _quorum_rule(require=2, approvers=("alice", "bob", "carol"), ttl_ms=None):
    return ApprovalRule("announce", ttl_ms, require, tuple(approvers))


# ---------------------------------------------------------------------------
# list / show: the golden read surface
# ---------------------------------------------------------------------------

def test_list_reports_a_pending_single_party_ticket_with_its_binding(tmp_path):
    session = _harness(tmp_path)
    ticket = _ticket(session)

    out = approvals_surface.list_approvals(session, pending=True)

    assert out["action"] == "list"
    assert out["session"] == session._session_id
    assert out["count"] == 1
    assert out["countsByState"] == {"pending": 1}
    row = out["tickets"][0]
    assert row["hash"] == ticket["hash"]
    assert row["effect"]["display"] == "ops.shout"
    assert row["effect"]["component"] == "Agent"
    assert row["candidateHash"] == ticket["candidateHash"]
    assert row["argsDigest"] == ticket["argsDigest"]
    assert row["round"] == 1
    assert row["state"]["state"] == "pending"
    assert row["raisers"] == ["alice"]
    assert "announce" in row["capabilities"]
    # the honest limit, not an invented number
    assert row["expiryRemainingMs"] is None
    assert "ageDetail" in row


def test_list_pending_excludes_a_consumed_ticket(tmp_path):
    session = _harness(tmp_path)
    ticket = _ticket(session)
    session.approve_ticket(ticket["hash"])
    assert _cross(session) is None            # the yes is spent

    out = approvals_surface.list_approvals(session, pending=True)

    assert out["count"] == 0
    every = approvals_surface.list_approvals(session)
    assert every["count"] == 1
    assert every["tickets"][0]["state"]["state"] == "consumed"


def test_list_reports_one_asking_answered_while_another_still_waits(tmp_path):
    """Issue #3216: the same question can be asked by two operators; one yes
    answers one asking. The surface says the QUESTION is still pending and
    shows the grant that stands for the other asking."""
    session = _harness(tmp_path)
    ticket = _ticket(session)
    session.operator = session.operator_registry.get("bob")
    with pytest.raises(ApprovalRequired):
        session._approval_decide_call("ops", "shout", ["sink.log", "a"])

    session.operator = session.operator_registry.get("alice")
    session.approve_ticket(ticket["hash"], proposer="alice")

    out = approvals_surface.list_approvals(session, pending=True)
    row = out["tickets"][0]
    assert row["state"]["state"] == "pending"
    assert row["raisers"] == ["bob"]
    assert row["state"]["answeredGrants"] == 1
    assert row["state"]["latestGrant"] == "granted"


def test_show_renders_body_policy_bindings_advice_and_diff(tmp_path):
    session = _harness(tmp_path)
    ticket = _ticket(session)

    out = approvals_surface.show_approval(session, ticket["hash"], diff=True)

    assert out["action"] == "show"
    assert out["body"]["hash"] == ticket["hash"]
    assert out["policy"]["approvalPolicy"] == "auto"
    assert out["bindings"]["hash"] == ticket["hash"]
    assert out["bindings"]["candidateHash"] == ticket["candidateHash"]
    assert out["bindings"]["session"] == session._session_id
    assert out["advice"]["model"] is None
    assert any(action["verb"] == "revl_approve"
               for action in out["advice"]["actions"])
    diff = out["diff"]
    assert diff["effectClass"] == "c"
    assert diff["externalEffects"] >= 1
    assert diff["oneWayWithoutInverse"] >= 1
    assert diff["crossings"][0]["classification"]
    assert "announce" in diff["capabilityWidenings"]["capabilities"]


def test_show_refuses_an_unknown_hash_and_list_refuses_another_session(tmp_path):
    session = _harness(tmp_path)
    with pytest.raises(ValueError, match="unknown ticket hash"):
        approvals_surface.show_approval(session, "not-a-ticket")
    with pytest.raises(ValueError, match="live session"):
        approvals_surface.list_approvals(session, session_id="another-session")


_APPROVAL_TABLES = ("_tickets", "_ticket_rounds", "_ticket_pending_raisers",
                    "_ticket_refusals", "_quorums", "_ledger",
                    "_approval_records", "_ticket_proposers", "_grants")


def _approval_state(session) -> tuple:
    tables = {name: copy.deepcopy(getattr(session, name, None))
              for name in _APPROVAL_TABLES}
    wal = Path(session._approval_wal().path)
    return tables, wal.read_bytes() if wal.exists() else None


def test_the_tool_is_read_only_and_returns_the_list_shape(tmp_path, monkeypatch):
    session = _harness(tmp_path, rules=[_quorum_rule()])
    ticket = _ticket(session)
    monkeypatch.setattr(server, "SESSION", session)
    for name in _APPROVAL_TABLES[:7]:          # the tables the surface reads
        assert hasattr(session, name), name
    before = _approval_state(session)

    out = server._tool_approvals({"action": "list"})
    assert out["ok"] is True and out["action"] == "list"
    assert out["count"] == 1
    assert _approval_state(session) == before
    for args in ({"action": "list", "pending": True},
                 {"action": "show", "hash": ticket["hash"], "diff": True},
                 {"action": "stats"}):
        assert server._tool_approvals(args)["ok"] is True, args
        assert _approval_state(session) == before, args


@pytest.mark.parametrize("bad_hash", [["sha256:x"], {"hash": "sha256:x"}, 7])
def test_show_refuses_a_hash_that_is_not_a_string(tmp_path, monkeypatch,
                                                   bad_hash):
    """A non-string `hash` is an argument error, never an unhandled
    `TypeError` from the ticket-table lookup."""
    session = _harness(tmp_path)
    _ticket(session)
    monkeypatch.setattr(server, "SESSION", session)

    out = server._tool_approvals({"action": "show", "hash": bad_hash})

    assert out["ok"] is False
    assert "must be a string" in out["diagnostics"][0]["message"]


def test_the_tool_is_gated_on_the_approve_verb(tmp_path):
    session = _harness(tmp_path)
    _ticket(session)

    allowed = op.parse_profile(
        "operator bob may approve on *").get("bob")
    denied = op.parse_profile("operator bob may swap on *").get("bob")

    session.operator = allowed
    assert op.decide(session, "revl_approvals", {"action": "list"}).allowed is True
    session.operator = denied
    decision = op.decide(session, "revl_approvals", {"action": "list"})
    assert decision.gated is True and decision.allowed is False


# Two components, each with its own class-(c) emission, so a ticket, a grant,
# a capability and a distillation rule can each resolve to exactly ONE of them.
_TWO_SOURCE = _SOURCE + (
    "extern emission fn wire(dest: Str, amount: Str) = @py {\n"
    "    return\n"
    "}\n"
    "service Pay {\n"
    "  emission fn pay(dest: Str, amount: Str)\n"
    "}\n"
    "component Bank provides pay: Pay {\n"
    "  provide pay {\n"
    "    fn pay(dest, amount) { emit wire(dest, amount) }\n"
    "  }\n"
    "}\n"
)

_SCOPED = "operator bob may approve on Agent"


def _two_component_harness(tmp_path):
    session = _harness(tmp_path)
    ir = copy.deepcopy(compile_source(_TWO_SOURCE, "approvals2991two.rvl"))
    session.recorder = _replay.Recorder(copy.deepcopy(ir))
    session.ir = ir
    session._class_map = ClassMap(ir)
    tickets = {}
    for service, method in (("ops", "shout"), ("pay", "pay")):
        with pytest.raises(ApprovalRequired) as caught:
            session._approval_decide_call(service, method, ["x.log", "m"])
        tickets[caught.value.ticket["component"]] = caught.value.ticket["hash"]
    return session, tickets


class _Offer:
    class rule:  # noqa: N801 - the shape `_distillation_targets` reads
        component = "Agent"


def _agent_target_arguments(session, tickets) -> dict:
    """One argument per key `_approve_targets` reads, each resolving to Agent."""
    session._grants.append({"requestId": "grant-agent-1", "component": "Agent"})
    session._offer_by_id = lambda offer_id: _Offer if offer_id == "offer-1" \
        else None
    return {"hash": tickets["Agent"], "requestId": "grant-agent-1",
            "capability": "announce", "offerId": "offer-1",
            "rule": "component Agent may auto-approve announce"}


@pytest.mark.parametrize("action", ["list", "stats"])
@pytest.mark.parametrize("key", ["hash", "requestId", "capability", "offerId",
                                 "rule"])
def test_list_and_stats_need_the_whole_composition_whatever_key_is_added(
        tmp_path, action, key):
    """`list` and `stats` read every component's tickets, so a grant scoped to
    one component never authorizes them, even when the arguments carry a key
    that would scope a `revl_approve` to that component."""
    session, tickets = _two_component_harness(tmp_path)
    value = _agent_target_arguments(session, tickets)[key]
    agent = [t[0] for t in op._approve_targets(session, {key: value}) or []]
    assert agent == ["Agent"]   # the key alone WOULD scope an approve to Agent

    session.operator = op.parse_profile(_SCOPED).get("bob")
    decision = op.decide(session, "revl_approvals",
                         {"action": action, key: value})
    assert decision.gated is True and decision.allowed is False

    session.operator = session.operator_registry.get("alice")  # `on *`
    assert op.decide(session, "revl_approvals",
                     {"action": action, key: value}).allowed is True


def test_show_scopes_to_the_ticket_component_and_hides_other_tickets(tmp_path):
    """A scoped operator may `show` a ticket of its own component. A ticket of
    another component is refused with exactly the refusal an unknown hash gets,
    so the refusal is no oracle for which tickets exist or whose they are."""
    session, tickets = _two_component_harness(tmp_path)
    session.operator = op.parse_profile(_SCOPED).get("bob")

    own = op.decide(session, "revl_approvals",
                    {"action": "show", "hash": tickets["Agent"]})
    assert own.allowed is True

    other = op.decide(session, "revl_approvals",
                      {"action": "show", "hash": tickets["Bank"]})
    unknown = op.decide(session, "revl_approvals",
                        {"action": "show", "hash": "sha256:" + "0" * 64})
    assert other.allowed is False and unknown.allowed is False
    assert other.message == unknown.message
    assert other.why.to_json() == unknown.why.to_json()
    assert "Bank" not in other.message
    assert "Bank" not in repr(other.why.to_json())
    # the other keys do not widen `show` either
    extra = _agent_target_arguments(session, tickets)
    for key in ("requestId", "capability", "offerId", "rule"):
        widened = op.decide(session, "revl_approvals",
                            {"action": "show", "hash": tickets["Bank"],
                             key: extra[key]})
        assert widened.allowed is False and widened.message == unknown.message


# ---------------------------------------------------------------------------
# stats: counts, decision times, override and escalate history
# ---------------------------------------------------------------------------

def test_stats_counts_states_and_times_and_lists_overrides(tmp_path):
    clock = {"now": 1_000}
    session = _harness(tmp_path, rules=[_quorum_rule(ttl_ms=60_000)],
                       clock=clock)
    clock["now"] = 1_000
    first = _ticket(session, args=("sink.log", "one"))
    clock["now"] = 1_100
    session.override_ticket(first["hash"], reason="approvers unreachable")

    clock["now"] = 2_000
    second = _ticket(session, args=("sink.log", "two"))
    clock["now"] = 2_300
    session.escalate_ticket(second["hash"], reason="hand it up")

    out = approvals_surface.stats(session)

    assert out["action"] == "stats"
    assert out["counts"]["satisfied"] == 1
    assert out["counts"]["satisfiedByOverride"] == 1
    assert out["counts"]["escalated"] == 1
    assert out["averageTimeToDecisionMs"] == pytest.approx(200.0)
    assert out["decisionsMeasured"] == 2
    assert out["overrides"][0]["reason"] == "approvers unreachable"
    assert out["escalations"][0]["reason"] == "hand it up"
    # the in-memory mirror and the durable log are de-duplicated, not doubles
    assert out["sources"]["merged"] == len(
        session._approval_records) + (
            out["sources"]["durable"] - out["sources"]["inMemory"])


# ---------------------------------------------------------------------------
# E-Stop: every pending question is revoked, distinctly from expired
# ---------------------------------------------------------------------------

def test_estop_revokes_a_pending_single_party_ticket_and_refuses_replay(tmp_path):
    session = _harness(tmp_path)
    ticket = _ticket(session)

    halt = _halt(session)

    assert halt["halted"] is True
    assert [row["hash"] for row in halt["revokedApprovals"]] == [ticket["hash"]]
    assert halt["revokedApprovals"][0]["durableRecord"] == "ticket-revoked"
    assert "ticket-revoked" in _kinds(session)
    # the durable row is what `revl recover` reports, end to end
    report = recovery.recover(session._approval_wal().path)
    assert [row["hash"] for row in report["revokedTickets"]] == [ticket["hash"]]
    assert report["revokedTickets"][0]["state"] == "revoked"
    assert report["revokedTickets"][0]["estop"] is True
    # the ticket reads revoked, not closed and not expired
    shown = approvals_surface.show_approval(session, ticket["hash"])
    assert shown["ticket"]["state"]["state"] == "revoked"
    assert shown["refusal"]["estop"] is True
    # a replay of the question is the DISTINCT refusal class, not a new prompt
    with pytest.raises(ApprovalRefused) as refused:
        session._approval_decide_call("ops", "shout", ["sink.log", "a"])
    assert refused.value.code == "APPROVAL_REFUSED"
    # and no yes can follow the halt
    with pytest.raises(SessionError, match="E-STOPPED"):
        session.approve_ticket(ticket["hash"])


def test_estop_closes_every_open_quorum_with_a_revoked_row(tmp_path):
    session = _harness(tmp_path, rules=[_quorum_rule()])
    ticket = _ticket(session)

    halt = _halt(session)

    assert [row["kind"] for row in halt["revokedApprovals"]] == ["quorum"]
    assert "quorum-revoked" in _kinds(session)
    record = session._quorums[session._quorum_request_id(ticket["hash"])]
    assert record["outcome"] == "revoked"
    shown = approvals_surface.show_approval(session, ticket["hash"])
    assert shown["ticket"]["state"]["state"] == "revoked"
    # a late vote is refused, and the session's dead latch refuses approval
    with pytest.raises(SessionError):
        session.approve_ticket(ticket["hash"], as_token="bob",
                               as_secret=_SECRETS["bob"])
    with pytest.raises(SessionError):
        session.override_ticket(ticket["hash"], reason="too late")


def test_estop_closes_a_named_single_approver_rule_as_a_quorum(tmp_path):
    """`require 1 of {a, b}` names approvers without demanding several, so the
    ticket still opens a decision graph; the sweep must close that graph rather
    than refuse only the asking and leave the graph open."""
    session = _harness(tmp_path, rules=[_quorum_rule(require=1)])
    ticket = _ticket(session)

    halt = _halt(session)

    assert [row["kind"] for row in halt["revokedApprovals"]] == ["quorum"]
    record = session._quorums[session._quorum_request_id(ticket["hash"])]
    assert record["outcome"] == "revoked"
    assert "ticket-revoked" not in _kinds(session)


def test_estop_leaves_a_consumed_grant_and_its_question_alone(tmp_path):
    session = _harness(tmp_path)
    ticket = _ticket(session)
    session.approve_ticket(ticket["hash"])
    assert _cross(session) is None
    entry = session._ledger[-1]
    assert entry["consumed"] is True

    halt = _halt(session)

    assert entry["consumed"] is True and not entry.get("revoked")
    assert halt["revokedApprovals"] == []
    assert "ticket-revoked" not in _kinds(session)


def test_estop_refuses_approve_cast_and_override_after_the_halt(tmp_path):
    session = _harness(tmp_path, rules=[_quorum_rule()])
    ticket = _ticket(session)
    _halt(session)

    for call in (
            lambda: session.approve_ticket(ticket["hash"]),
            lambda: session._cast_vote(ticket, _quorum_rule(),
                                       vote="approve", as_token="bob",
                                       as_secret=_SECRETS["bob"]),
            lambda: session.override_ticket(ticket["hash"], reason="late")):
        with pytest.raises(SessionError, match="E-STOPPED"):
            call()


def test_a_lapsed_quorum_is_expired_not_revoked(tmp_path):
    clock = {"now": 1_000}
    session = _harness(tmp_path, rules=[_quorum_rule(ttl_ms=100)],
                       clock=clock)
    ticket = _ticket(session)
    clock["now"] = 10_000                  # past the deadline, never voted

    halt = _halt(session)

    # the sweep leaves a lapsed question to the expiry vocabulary
    assert halt["revokedApprovals"] == []
    shown = approvals_surface.show_approval(session, ticket["hash"])
    assert shown["ticket"]["state"]["state"] == "expired"
    # ... and closes it durably as expired, so `revl recover` does not read a
    # bare `quorum-open` as a question neither revoked nor expired
    from revl.wal import expired_tickets, revoked_tickets
    records = _records(session)
    assert _kinds(session).count("quorum-expired") == 1
    assert "quorum-revoked" not in _kinds(session)
    assert [row["hash"] for row in expired_tickets(records)] == [ticket["hash"]]
    assert revoked_tickets(records) == []
    _halt(session)                      # a second halt writes nothing more
    assert _kinds(session).count("quorum-expired") == 1


# ---------------------------------------------------------------------------
# recover: revoked is distinct from expired
# ---------------------------------------------------------------------------

def test_recover_reports_a_revoked_ticket_distinct_from_an_expired_one(tmp_path):
    log = _replay.WriteAheadLog(str(tmp_path / "run.wal"))
    log.open()
    log.record_ticket_revoked({
        "hash": "revoked-hash", "component": "Agent",
        "candidateHash": "cand-1", "kind": "call", "round": 1,
        "proposers": ["alice"], "by": "alice", "reason": "operator halt",
        "estop": True, "at": 200, "state": "revoked"})
    log.record_quorum_event("quorum-expired", {
        "requestId": "expired-id", "hash": "expired-hash",
        "candidateHash": "cand-2", "component": "Payments", "kind": "call",
        "round": 1, "openedAt": 100, "expiresAt": 150, "resolvedAt": 160,
        "counted": 0, "require": 2})
    log.close()

    report = recovery.recover(str(tmp_path / "run.wal"))

    assert report["revokedTickets"][0]["state"] == "revoked"
    assert report["revokedTickets"][0]["hash"] == "revoked-hash"
    assert report["revokedTickets"][0]["estop"] is True
    assert report["expiredTickets"][0]["state"] == "expired"
    assert report["expiredTickets"][0]["hash"] == "expired-hash"
    rendered = recovery.render(report)
    assert "REVOKED" in rendered and "revoked-hash" in rendered
    assert "expired" in rendered and "expired-hash" in rendered

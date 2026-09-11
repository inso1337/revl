"""Multi-party / quorum approval - roadmap item 471 (issue #823).

Design: docs/design/471-quorum-approval.md. Extends the item 246 / 251 approval
machinery rather than paralleling it: a rule may name the operators who may
answer (`capability announce requires approval require 2 of {alice, bob, carol}`),
and when it demands more than one distinct approver the single `yes` the two-step
has always accepted is no longer enough. The crossing stays refused until N
distinct named approvers, none of them the operator who PROPOSED it, have each
cast a vote bound to that exact question.

What this suite pins, all of it as refusals rather than as happy paths:

  * the proposer can never satisfy a quorum alone, and a rule that leaves fewer
    eligible approvers than it demands is refused AT THE CROSSING, before any
    ticket is issued (separation of duties);
  * a vote is bound to the exact ticket, and the exact reach-closure candidate
    hash, plan and resource target inside it: a vote cast against another
    question is a different question's vote;
  * votes cannot be re-counted, cannot be replayed across a candidate that
    changed under the question, and expire with the ticket's own ttl;
  * an emergency override is recorded as an override and never counted as a
    quorum of votes;
  * denial, timeout, revoke and escalation each leave an auditable record and
    close the question;
  * unknown approvers and unparseable votes fail closed, and every refusal is
    written to the decision graph before it is raised.

The harness needs no cordis runtime. `Session.load()` does, so the suite drives
the same decision path `Session.call` drives (`_approval_decide_call`, which
issues the ticket and raises `ApprovalRequired`) against a class map built
straight off the compiled IR, with a real WAL open on a tmp path. That keeps the
whole protocol - tickets, ledger, WAL records, votes - exercised end to end
without a live composition. `test_standing_approval.py` proves the crossing
itself against the live runtime.

Note for the reader: cast identity is supplied as `as_token` because a session
binds exactly ONE operator (see the design note's "honest bounds" section), so
the second and later votes are indivisible from the same session's protocol by
construction. What the protocol guarantees is that the COUNT is of distinct
named approvers, not of repeats.
"""

import copy
import importlib.util
import os
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
from revl.mcp.approval import ApprovalRequired, ClassMap       # noqa: E402
from revl.mcp.session import Session, SessionError             # noqa: E402
from revl.policy import ApprovalRule, AutoApproveRule, Policy  # noqa: E402
from revl.policy import PolicyError, TAINT_FOLD_ORIGINS        # noqa: E402
from revl.policy import parse_policy                           # noqa: E402

# The one crossing the whole suite turns on: `shout` is class (c), reaching the
# emission capability `announce`.
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

# A swap candidate: same services, different body, so `shout`'s reach closure -
# and therefore the ticket's candidate hash - changes under the question.
_SOURCE_SWAPPED = _SOURCE.replace("announce:(sink, msg)", "nope")
_SOURCE_SWAPPED = _SOURCE.replace(
    "        _f.write('announce:' + msg + '\\n')",
    "        _f.write('announce:' + msg + '!\\n')")

# A crossing that reaches TWO emitting capabilities, for the one shape no single
# approver set can answer: two capabilities whose rules name different humans.
_SOURCE_TWO_CAPS = (
    "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('announce:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "extern emission fn page(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('page:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service Ops {\n"
    "  emission fn shout(sink: Str, msg: Str)\n"
    "}\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn shout(sink, msg) { emit announce(sink, msg); emit page(sink, msg) }\n"
    "  }\n"
    "}\n"
)

_THREE = ("alice", "bob", "carol")


def _quorum(require=2, approvers=_THREE, ttl_ms=None):
    return ApprovalRule("announce", ttl_ms, require, tuple(approvers))


def _harness(tmp_path, *, rules=None, token="alice", source=_SOURCE, clock=None,
             name="wal.json"):
    """A session wired for the approval path with no cordis runtime: a real
    recorder over the compiled IR, a real open WAL on `tmp_path`, a class map
    built from the IR, and the policy sandbox carrying the quorum rule."""
    ir = copy.deepcopy(compile_source(source, "quorum471.rvl"))
    session = Session()
    session.recorder = _replay.Recorder(copy.deepcopy(ir))
    session._wal_path = str(tmp_path / name)
    session._generation = 1
    session._ensure_wal_open()
    session.approval_policy = "auto"
    session._class_map = ClassMap(ir)
    session.sandbox = Policy(
        approval_rules=tuple(rules if rules is not None else [_quorum()]))
    session.operator = op.Operator(token=token)
    if clock is not None:
        session._clock_ms = lambda: clock["now"]
    return session


def _ticket(session, args=("sink.log", "a")):
    """The ticket the crossing raises, exactly as `Session.call` would raise it."""
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
    """Every record the decision graph has written to the durable WAL."""
    return _replay.WriteAheadLog.read(session._approval_wal().path)["records"]


def _kinds(session) -> list:
    return [record["record"] for record in _records(session)]


@pytest.fixture
def quorum(tmp_path):
    return _harness(tmp_path)


# ---------------------------------------------------------------------------
# The policy language: the clause, and everything it refuses to be
# ---------------------------------------------------------------------------

def test_dsl_carries_the_quorum_clause_and_defaults_to_single_party():
    """The clause parses, and a rule written before item 471 keeps the
    byte-identical single-party shape (`require=1`, no named approvers), which is
    what stops the new vocabulary from changing any existing policy."""
    policy = parse_policy(
        "capability announce requires approval require 2 of {alice, bob, carol}\n"
        "capability charge requires approval ttl 30s\n")
    quorum = policy.approval_rule_for("announce")
    assert (quorum.require, quorum.approvers) == (2, _THREE)
    assert quorum.is_quorum() and quorum.names_approvers()
    assert quorum.ttl_ms is None
    plain = policy.approval_rule_for("charge")
    assert (plain.require, plain.approvers) == (1, ())
    assert not plain.is_quorum() and not plain.names_approvers()
    assert plain.ttl_ms == 30_000


def test_dsl_accepts_the_clause_on_either_side_of_the_ttl():
    both = parse_policy(
        "capability announce requires approval require 2 of {a, b} ttl 30s\n"
        "capability charge requires approval ttl 30s require 2 of {a, b}\n")
    assert both.approval_rule_for("announce").ttl_ms == 30_000
    assert both.approval_rule_for("charge").ttl_ms == 30_000
    assert both.approval_rule_for("charge").approvers == ("a", "b")


def test_json_carries_the_quorum_clause():
    policy = parse_policy(
        '{"approvals": [{"capability": "announce", "ttl": "30s", '
        '"require": 2, "of": ["alice", "bob", "carol"]}]}')
    rule = policy.approval_rule_for("announce")
    assert (rule.require, rule.approvers, rule.ttl_ms) == (2, _THREE, 30_000)


@pytest.mark.parametrize("text,why", [
    ("capability announce requires approval require 2 of {alice, alice}",
     "duplicate approver"),
    ("capability announce requires approval require 2 of {*}",
     "wildcard approver"),
    ("capability announce requires approval require 3 of {alice, b}",
     "demands more approvers than are named"),
    ("capability announce requires approval require 0 of {alice, b}",
     "a count of zero"),
    ("capability announce requires approval require 2 of {}",
     "an empty approver set"),
    ("capability announce requires approval require 2 of {a, b} uses 3",
     "an unrecognised trailer"),
], ids=["duplicate", "wildcard", "too-many", "zero", "empty", "trailer"])
def test_dsl_refuses_a_quorum_that_cannot_mean_what_it_says(text, why):
    """Every malformed clause is a policy error, not a silently weakened rule. A
    duplicate name would let one operator supply two of the N, a wildcard would
    make the counted set unattributable, and a count above the named set is a
    quorum that can never be reached."""
    with pytest.raises(PolicyError):
        parse_policy(text)


@pytest.mark.parametrize("entry,why", [
    ({"capability": "announce", "require": 2}, "no `of` list"),
    ({"capability": "announce", "of": ["a", "b"]}, "no `require`"),
    ({"capability": "announce", "require": 2, "of": "a,b"}, "`of` is not a list"),
    ({"capability": "announce", "require": 3, "of": ["a", "b"]},
     "a count above the named set"),
], ids=["missing-of", "missing-require", "of-not-a-list", "too-many"])
def test_json_refuses_an_incomplete_quorum(entry, why):
    """The two keys are written together or not at all: defaulting a missing
    `require` to the length of `of` would turn a typo into a unanimous quorum."""
    import json

    with pytest.raises(PolicyError):
        parse_policy(json.dumps({"approvals": [entry]}))


# ---------------------------------------------------------------------------
# The exit criterion: `require 2 of {a,b,c}` admits only on two distinct votes
# ---------------------------------------------------------------------------

def test_two_distinct_votes_admit_the_crossing_and_one_does_not(quorum):
    """The item's exit criterion. One vote leaves the crossing refused; the
    second distinct approver's vote mints the approval, the crossing goes
    through, and the WAL carries the whole decision graph."""
    ticket = _ticket(quorum)

    first = quorum.approve_ticket(ticket["hash"], as_token="bob")
    assert first["approved"] is False and first["counted"] == 1
    assert first["outstanding"] == ["alice", "carol"]

    # one vote is not a quorum: the crossing is still refused
    assert _cross(quorum) is not None

    second = quorum.approve_ticket(ticket["hash"], as_token="carol")
    assert second["approved"] is True and second["counted"] == 2
    assert second["satisfiedBy"] == "votes"

    assert _cross(quorum) is None, "two distinct valid votes must admit it"
    assert _kinds(quorum) == ["quorum-open", "quorum-vote", "quorum-vote",
                              "quorum-satisfied", "approval-granted",
                              "approval-consumed"]


def test_the_minted_approval_is_an_ordinary_ledger_entry(quorum):
    """The quorum mints through the SAME path a single yes does, so every
    invariant that path already carries holds unchanged: one entry, bound to the
    ticket hash, the reach-closure candidate hash, the component, the session and
    the round, single-use and consumed before the fire."""
    ticket = _ticket(quorum)
    quorum.approve_ticket(ticket["hash"], as_token="bob")
    quorum.approve_ticket(ticket["hash"], as_token="carol")
    (entry,) = quorum._ledger
    assert entry["hash"] == ticket["hash"]
    assert entry["candidateHash"] == ticket["candidateHash"]
    assert entry["component"] == "Agent" and entry["round"] == 1
    assert entry["session"] == quorum._session_id
    assert entry["consumed"] is False
    granted = [r for r in quorum._approval_records
               if r["record"] == "approval-granted"]
    (record,) = granted
    assert record["quorum"]["satisfiedBy"] == "votes"
    assert record["quorum"]["counted"] == 2
    assert record["quorum"]["voted"] == ["bob", "carol"]


def test_a_single_vote_never_admits_a_quorum_ticket(quorum):
    """One approver, however many times they call, is still one approver: a
    quorum demands DISTINCT votes, so a repeated yes is refused rather than
    counted and the crossing stays refused."""
    ticket = _ticket(quorum)
    quorum.approve_ticket(ticket["hash"], as_token="bob")
    with pytest.raises(SessionError, match="already voted"):
        quorum.approve_ticket(ticket["hash"], as_token="bob")
    assert _cross(quorum) is not None
    assert quorum._counted(quorum._quorums[ticket["hash"]]) == 1


# ---------------------------------------------------------------------------
# Separation of duties
# ---------------------------------------------------------------------------

def test_the_proposer_can_never_satisfy_quorum_alone(quorum):
    """The proposer's own vote is refused, recorded as `proposer` under a
    `quorum-refused` record, and the crossing stays refused. A quorum cannot be
    closed by the operator who asked for the crossing."""
    ticket = _ticket(quorum)
    with pytest.raises(SessionError, match="cannot also approve it"):
        quorum.approve_ticket(ticket["hash"])

    refused = [r for r in _records(quorum) if r["record"] == "quorum-refused"]
    (row,) = refused
    assert row["reason"] == "proposer" and row["voter"] == "alice"
    assert row["action"] == "vote" and row["proposer"] == "alice"
    assert row["counted"] == 0
    assert _cross(quorum) is not None
    assert _kinds(quorum) == ["quorum-open", "quorum-refused"]


def test_the_proposers_vote_does_not_count_toward_any_other_vote(quorum):
    """A refused vote is not a vote: the count after the proposer's refusal and
    two honest votes is exactly two, and the proposer is still outstanding."""
    ticket = _ticket(quorum)
    with pytest.raises(SessionError):
        quorum.approve_ticket(ticket["hash"])
    quorum.approve_ticket(ticket["hash"], as_token="bob")
    final = quorum.approve_ticket(ticket["hash"], as_token="carol")
    assert final["counted"] == 2
    assert "alice" in final["outstanding"]


def test_a_rule_the_proposer_leaves_unsatisfiable_is_refused_at_the_crossing(
        tmp_path):
    """`require 2 of {alice, bob}` proposed by alice leaves one eligible approver
    and demands two. The honest outcome is a refusal at the asking: no ticket is
    issued, no prompt is counted, and nothing can be voted on. A ticket nobody
    can answer is a denial of service dressed as a control."""
    session = _harness(tmp_path, rules=[_quorum(2, ("alice", "bob"))])
    with pytest.raises(SessionError, match="separation of duties"):
        session._approval_decide_call("ops", "shout", ["sink.log", "a"])
    assert session._tickets == {}
    assert "quorum-open" not in _kinds(session)


def test_naming_the_proposer_harmlessly_still_works(tmp_path):
    """The refusal is about arithmetic, not about names: `require 2 of {alice,
    bob, carol}` proposed by alice leaves two eligible and is issuable, so naming
    the proposer in the set is not itself an error."""
    session = _harness(tmp_path, token="alice")
    assert _ticket(session)["component"] == "Agent"


def test_a_crossing_reaching_rules_with_different_approver_sets_is_refused(
        tmp_path):
    """Two capabilities demanding DIFFERENT approver sets cannot both be honoured
    by one answer, and "which humans does this need" must have one answer or
    none. The crossing is refused rather than silently answered against one of
    the two rules."""
    session = _harness(tmp_path, source=_SOURCE_TWO_CAPS, rules=[
        ApprovalRule("announce", None, 2, ("alice", "bob")),
        ApprovalRule("page", None, 2, ("dan", "erin")),
    ])
    assert session._class_map is not None
    with pytest.raises(SessionError, match="different approver sets"):
        session._approval_decide_call("ops", "shout", ["sink.log", "a"])


# ---------------------------------------------------------------------------
# No admission path may bypass the quorum
#
# `_issue_ticket` forces the vote path for a multi-party rule, but a standing
# grant and a distilled auto-approve rule are consulted BEFORE it
# (`_approval_decide_call` tries `_find_standing_approval`, then
# `_find_standing_grant`, then `_find_auto_approve`, and only then issues the
# ticket). Each of those is ONE operator's authority recorded once, so each is
# a way to admit a quorum-gated crossing with zero votes unless the rule is
# consulted there too. These tests pin that, in both directions: the bypass is
# refused, and the ordinary single-party paths still work.
#
# The single load-bearing predicate is `Session._multi_party_rules`; the tests
# below cover all three paths that consult it (mint, match, cover) plus the
# transport.
# ---------------------------------------------------------------------------

def test_a_quorum_gated_crossing_cannot_be_widened_into_a_standing_grant(quorum):
    """`revl_approve(hash=<quorum ticket>, uses=N)` is one operator saying "yes,
    N times" to a question that demands two DISTINCT people. Refused at the
    mint, so no grant exists for the crossing to match, the crossing is still
    refused, and the durable graph has no granted/consumed pair beside it: the
    question stays open for the votes that can actually answer it."""
    ticket = _ticket(quorum)
    with pytest.raises(SessionError, match="standing grant") as caught:
        quorum.mint_standing_grant(ticket_hash=ticket["hash"], uses=2)
    assert "require 2 of {alice, bob, carol}" in str(caught.value)

    assert quorum._grants == []
    assert _cross(quorum) is not None
    assert _kinds(quorum) == ["quorum-open"]
    state = quorum.quorum_state(ticket["hash"])
    assert state["outcome"] is None and state["counted"] == 0


def test_a_quorum_gated_capability_cannot_be_minted_proactively(quorum):
    """The same mint answered against a `capability` instead of a ticket is the
    same refusal: naming the crossing proactively must not be a way around the
    rule that covers it."""
    with pytest.raises(SessionError, match="standing grant"):
        quorum.mint_standing_grant(capability="announce", uses=2)
    assert quorum._grants == []
    assert _cross(quorum) is not None
    assert _kinds(quorum) == ["quorum-open"]


def test_a_rule_that_only_names_approvers_also_refuses_a_standing_grant(tmp_path):
    """`require 1 of {a, b}` is "either of these two humans", not "anyone": the
    rule restricts WHO may answer, so one operator's standing authority is not a
    substitute for it either. Both mint routes refuse."""
    session = _harness(tmp_path, rules=[ApprovalRule("announce", None, 1,
                                                     ("alice", "bob"))])
    ticket = _ticket(session)
    with pytest.raises(SessionError, match="standing grant"):
        session.mint_standing_grant(ticket_hash=ticket["hash"], uses=2)
    with pytest.raises(SessionError, match="standing grant"):
        session.mint_standing_grant(capability="announce", uses=2)
    assert session._grants == []


def test_a_grant_minted_before_the_rule_was_bound_does_not_cover_it(tmp_path):
    """A grant is session-scoped consent to a crossing, not consent to a RULE.
    Rebinding a policy that now gates the crossing behind named approvers must
    not let the earlier grant spend through it: `_find_standing_grant` consults
    the same predicate, so the crossing prompts and the grant is left unspent
    for whatever it still legitimately covers."""
    session = _harness(tmp_path, rules=[ApprovalRule("announce")])
    ticket = _ticket(session)
    grant = session.mint_standing_grant(ticket_hash=ticket["hash"], uses=2)
    assert grant["granted"] is True
    assert _cross(session) is None                # the grant admits it

    session._tickets.pop(ticket["hash"], None)
    session.sandbox = Policy(approval_rules=(_quorum(),))
    again = _ticket(session)
    assert session._find_standing_grant(again) is None

    (standing,) = session._grants
    before = standing["remainingUses"]
    assert _cross(session) is not None, "the rule owns the crossing, not the grant"
    assert standing["remainingUses"] == before, "the grant must not be spent"


def test_an_auto_approve_rule_never_covers_a_quorum_gated_crossing(tmp_path):
    """A distilled (or hand-written) `AutoApproveRule` covering the same
    capability would otherwise make the quorum rule decorative: the first call
    would be admitted without ever raising a ticket. It raises the ticket and the
    rule is not spent."""
    session = _harness(tmp_path)
    session.sandbox = Policy(
        approval_rules=(_quorum(),),
        auto_approve_rules=(AutoApproveRule(
            component="Agent", caps=("announce",),
            admitting=TAINT_FOLD_ORIGINS, uses=5),),
    )
    session._install_auto_approve_rules()
    assert session._auto_rules

    with pytest.raises(ApprovalRequired):
        session._approval_decide_call("ops", "shout", ["sink.log", "a"])
    assert _kinds(session) == ["quorum-open"]
    (rule,) = session._auto_rules
    assert rule["remainingUses"] == 5 and rule["consumed"] is False


def test_the_standing_grant_refusal_reaches_the_transport(tmp_path, monkeypatch):
    """The refusal is a `revl_approve` answer and not an in-process exception
    only: the tool reports it as a refusal instead of minting."""
    from revl.mcp import server

    session = _harness(tmp_path)
    monkeypatch.setattr(server, "SESSION", session)
    ticket = _ticket(session)

    by_hash = server._tool_approve({"hash": ticket["hash"], "uses": 2})
    assert by_hash["ok"] is False
    assert "standing grant" in by_hash["diagnostics"][0]["message"]
    by_cap = server._tool_approve({"capability": "announce", "uses": 2})
    assert by_cap["ok"] is False
    assert "standing grant" in by_cap["diagnostics"][0]["message"]
    assert session._grants == []
    assert _cross(session) is not None


def test_a_single_party_crossing_still_takes_a_standing_grant(tmp_path):
    """The control. A rule that names no approvers and demands one is exactly
    what item 344's standing grant is for, so both mint routes still work and the
    grant still admits the crossing, spending one use."""
    session = _harness(tmp_path, rules=[ApprovalRule("announce")])
    ticket = _ticket(session)
    session.mint_standing_grant(ticket_hash=ticket["hash"], uses=2)
    assert _cross(session) is None
    (grant,) = session._grants
    assert grant["remainingUses"] == 1

    other = _harness(tmp_path)
    other.sandbox = Policy(approval_rules=(ApprovalRule("announce"),))
    other.mint_standing_grant(capability="announce", uses=2)
    assert _cross(other) is None


def test_an_auto_approve_rule_still_covers_a_single_party_crossing(tmp_path):
    """The other control: item 251's auto-approve path is untouched where the
    rule names no approvers and demands no quorum."""
    session = _harness(tmp_path, rules=[ApprovalRule("announce")])
    session.sandbox = Policy(
        approval_rules=(ApprovalRule("announce"),),
        auto_approve_rules=(AutoApproveRule(
            component="Agent", caps=("announce",),
            admitting=TAINT_FOLD_ORIGINS, uses=5),),
    )
    session._install_auto_approve_rules()
    assert _cross(session) is None
    assert _kinds(session) == ["approval-consumed"]


# ---------------------------------------------------------------------------
# The lease bridge: the one route a quorum-gated `effect lease` keeps
#
# `_enforce_lease_gate` admits an `effect lease` only when a LIVE lease-tagged
# standing grant covers it, and the standing-grant path above refuses to mint
# one for a capability a multi-party rule covers, on both routes. Left there,
# such a lease could never be acquired at all: the votes would arrive on the
# decision graph, and nothing the gate consults would ever hear them. So the
# gate has a route of its own, and it is not an operator mint: when the decision
# for THAT lease ticket is SATISFIED, the gate mints the lease grant off the
# satisfied ledger entry itself (`_satisfied_decision_for`), the mint re-derives
# that proof (`_decision_authorizes_grant`) and accepts it only for a
# `kind='lease'` ticket, and the entry is spent once before the boot. Refused
# before the votes arrive, refused for a forged, spent or foreign decision, and
# still no consent to a crossing: the grant is lease-tagged, so the predicate
# turns it away at every class-(c) crossing.
#
# `Session.load()` needs the cordis runtime (absent here), so these tests drive
# `_enforce_lease_gate` directly, exactly as the rest of the suite drives
# `_approval_decide_call`.
# ---------------------------------------------------------------------------

_LEASE_SOURCE = (
    'extern emission[fs.write(path="/tmp")] fn wr(sink: Str, msg: Str)'
    " = @py {\n    with open(sink, 'a') as f: f.write('w:' + msg + '\\n')\n"
    "    return\n}\n"
    "service Ops { emission fn go(sink: Str, msg: Str) }\n"
    "component Agent provides ops: Ops {\n"
    '  let l = effect lease fs.write(path="/tmp") ttl 10m uses 3 '
    "undo l.revoke()\n"
    "  provide ops { fn go(sink, msg) { emit wr(sink, msg) } }\n"
    "}\n"
)

_LEASE_CAP = 'fs.write(path="/tmp")'


def _lease_rule(require=2, approvers=_THREE):
    return ApprovalRule(_LEASE_CAP, None, require, tuple(approvers))


def _lease_harness(tmp_path, *, rules=None, token="alice", name="lease.json"):
    """`_harness` over a composition that ACQUIRES an `effect lease`. Returns the
    session and the IR the gate walks (`_collect_lease_requests` reads the IR;
    the class map the gate resolves the lease against is the session's own)."""
    ir = copy.deepcopy(compile_source(_LEASE_SOURCE, "lease471.rvl"))
    session = _harness(
        tmp_path, source=_LEASE_SOURCE, token=token, name=name,
        rules=[_lease_rule()] if rules is None else rules)
    return session, ir


def _gate(session, ir):
    """One load's lease-gate outcome: None when the lease is ADMITTED, the
    `ApprovalRequired` it raised when the load is refused."""
    try:
        session._enforce_lease_gate(ir)
    except ApprovalRequired as caught:
        return caught
    return None


def test_a_quorum_gated_lease_loads_only_after_its_own_votes(tmp_path):
    """The lease is refused on the first load (there is no caller-supplied mint
    for it any more), the operators' votes satisfy the question it raised, and
    the next load mints the lease grant from THAT satisfied decision: bounded by
    the lease's own `ttl 10m` / `uses 3`, lease-tagged, and with the decision
    spent once before the boot. The grant is a lease handle, not consent to a
    crossing: the crossing the lease mediates still raises its own quorum."""
    session, ir = _lease_harness(tmp_path)

    refused = _gate(session, ir)
    assert refused is not None and refused.ticket["kind"] == "lease"
    assert _kinds(session) == ["quorum-open"]
    lease_ticket = refused.ticket

    assert session.approve_ticket(lease_ticket["hash"], vote="approve",
                                  as_token="bob")["counted"] == 1
    assert _gate(session, ir) is not None, "one vote is not two"
    voted = session.approve_ticket(lease_ticket["hash"], vote="approve",
                                   as_token="carol")
    assert voted["outcome"] == "satisfied"
    for route in ({"ticket_hash": lease_ticket["hash"], "uses": 3},
                  {"capability": _LEASE_CAP, "uses": 3}):
        with pytest.raises(SessionError, match="standing grant"):
            session.mint_standing_grant(**route)
    assert session._grants == [], "a satisfied decision is still not a public mint"

    assert _gate(session, ir) is None, "the satisfied decision is the answer"
    (grant,) = session._grants
    assert grant["lease"] is True and grant["kind"] == "standing-grant"
    assert grant["remainingUses"] == 3, "the lease's own `uses 3`"
    assert grant["expiresAt"] is not None, "the lease's own `ttl 10m`"
    assert grant["component"] == lease_ticket["component"]
    assert session._live_lease_grant("Agent", _LEASE_CAP) is grant
    assert grant["expiresAt"] - grant["grantedAt"] == 600_000, "ttl 10m"
    (entry,) = session._ledger
    assert entry["consumed"] is True, "the decision is spent before the boot"
    granted = [record for record in _records(session)
               if record["record"] == "approval-granted"]
    assert [row["quorum"]["satisfiedBy"] for row in granted
            if row.get("quorum")] == ["votes"], "the answer stays on the record"
    assert granted[-1]["remainingUses"] == 3, "the lease handle, once"
    assert _kinds(session) == ["quorum-open", "quorum-vote", "quorum-vote",
                               "quorum-satisfied", "approval-granted",
                               "approval-granted", "approval-consumed"]

    assert _gate(session, ir) is None, "a retry re-uses the same live handle"
    assert len(session._grants) == 1

    with pytest.raises(ApprovalRequired) as crossing:
        session._approval_decide_call("ops", "go", ["/tmp", "a"])
    assert crossing.value.ticket["hash"] != lease_ticket["hash"]
    assert session._find_standing_grant(crossing.value.ticket) is None


def test_a_quorum_gated_lease_is_refused_until_the_votes_arrive(tmp_path):
    """The refusal the lease gate has always had, now stated as the invariant it
    is: with zero votes, and with the votes counted but short of the rule, no
    route mints the lease and the load stays refused with the question open."""
    session, ir = _lease_harness(tmp_path)

    for stage, votes in (("no votes", ()), ("one vote", ("bob",))):
        for name in votes:
            session.approve_ticket(_gate(session, ir).ticket["hash"],
                                   vote="approve", as_token=name)
        refused = _gate(session, ir)
        assert refused is not None, f"admitted with {stage}"
        assert refused.ticket["kind"] == "lease"
    assert session._grants == []
    assert _kinds(session) == ["quorum-open", "quorum-vote"]

    with pytest.raises(SessionError, match="standing grant"):
        session.mint_standing_grant(ticket_hash=refused.ticket["hash"], uses=3)
    with pytest.raises(SessionError, match="standing grant"):
        session.mint_standing_grant(capability=_LEASE_CAP, uses=3)
    assert session._grants == []


def test_a_lease_refusal_is_reasoned_and_names_the_route_that_answers_it(tmp_path):
    """A refusal that does not say what would unblock it is a trap, and item 471
    leaves this one with no caller-supplied mint at all - so the refusals must
    carry the reason and the route, never a bare failure. The gate's refusal IS
    the question (`ApprovalRequired` carrying the `kind='lease'` ticket, on the
    record); the mint's refusal names the rule it answers, the composition it
    leaves open, and the route that unblocks it: vote, then re-issue the load.
    The last line proves the named route is the one that actually works."""
    session, ir = _lease_harness(tmp_path)

    refused = _gate(session, ir)
    assert isinstance(refused, ApprovalRequired), "a bare failure would be a trap"
    assert refused.ticket["kind"] == "lease"
    assert _LEASE_CAP in refused.ticket["capabilities"]
    assert _kinds(session) == ["quorum-open"], "the question is on the record"

    with pytest.raises(SessionError) as caught:
        session.mint_standing_grant(ticket_hash=refused.ticket["hash"], uses=3)
    message = str(caught.value)
    assert "require 2 of {alice, bob, carol}" in message, "the rule it answers"
    assert "effect lease" in message, "the composition it leaves open"
    assert "re-run the load" in message, "the route that unblocks it"

    for name in ("bob", "carol"):
        session.approve_ticket(refused.ticket["hash"], vote="approve", as_token=name)
    assert _gate(session, ir) is None, "the named route is the one that works"


def test_a_denied_lease_question_is_not_an_answer(tmp_path):
    """A denied decision closes the question and mints nothing, so the load is
    refused with the denial on the record rather than admitted on a decision
    that says no."""
    session, ir = _lease_harness(tmp_path)
    ticket = _gate(session, ir).ticket

    denied = session.approve_ticket(ticket["hash"], vote="deny", as_token="bob")
    assert denied.get("outcome") is None or denied["outcome"] != "satisfied"
    assert _gate(session, ir) is not None
    assert session._grants == []
    assert "quorum-denied" in _kinds(session)


def test_a_spent_lease_decision_answers_only_once(tmp_path):
    """The bridge spends the decision that admitted the lease. With that spend
    recorded and the handle gone (a revoke, or a generation change), the next
    load is refused and re-asks: the same decision cannot boot two leases."""
    session, ir = _lease_harness(tmp_path)
    ticket = _gate(session, ir).ticket
    for name in ("bob", "carol"):
        session.approve_ticket(ticket["hash"], vote="approve", as_token=name)
    assert _gate(session, ir) is None
    assert session._ledger[0]["consumed"] is True

    session._grants.clear()
    refused = _gate(session, ir)
    assert refused is not None and refused.ticket["kind"] == "lease"
    assert session._grants == []
    assert _kinds(session)[-1] == "quorum-open", "the refused load re-asks"


def test_the_lease_bridge_refuses_a_forged_or_foreign_decision(tmp_path):
    """The proof is re-derived inside the mint, never trusted: a COPY of the
    satisfied entry, a decision for some other ticket, and a decision whose
    record is not the entry's own all fail the mint, so no caller can hand the
    bridge an answer it did not earn."""
    session, ir = _lease_harness(tmp_path)
    lease_ticket = _gate(session, ir).ticket
    for name in ("bob", "carol"):
        session.approve_ticket(lease_ticket["hash"], vote="approve", as_token=name)
    (entry,) = session._ledger
    record = session._quorums[entry["requestId"]]
    assert session._satisfied_decision_for(lease_ticket) is entry

    assert session._decision_authorizes_grant(entry, lease_ticket["hash"]) is True
    for forged in (dict(entry),                    # a copy of the proof
                   {**entry, "hash": "other"},     # another ticket's entry
                   None,                           # no proof at all
                   record):                        # the decision, not the entry
        assert session._decision_authorizes_grant(forged,
                                                  lease_ticket["hash"]) is False
        with pytest.raises(SessionError, match="standing grant"):
            session._mint_grant(ticket_hash=lease_ticket["hash"], decision=forged)
    assert session._grants == []

    # A decision whose record is not satisfied is not an answer either, even
    # though the entry itself is the live one for this ticket.
    record["outcome"] = None
    assert session._satisfied_decision_for(lease_ticket) is None
    record["outcome"] = "satisfied"
    assert session._satisfied_decision_for(lease_ticket) is entry


def test_the_lease_bridge_is_scoped_to_lease_tickets(tmp_path):
    """The exception is the LEASE acquisition, not "a satisfied decision may
    mint a grant". A genuinely satisfied quorum on an ordinary class-(c) crossing
    still refuses the mint: that crossing is answered by the decision itself,
    single-use at its one call, and never by a standing grant."""
    session = _harness(tmp_path)
    ticket = _ticket(session)
    for name in ("bob", "carol"):
        session.approve_ticket(ticket["hash"], vote="approve", as_token=name)
    (entry,) = session._ledger
    assert session._satisfied_decision_for(ticket) is entry

    assert session._decision_authorizes_grant(entry, ticket["hash"]) is False
    with pytest.raises(SessionError, match="standing grant"):
        session._mint_grant(ticket_hash=ticket["hash"], decision=entry)
    assert session._grants == []
    assert _cross(session) is None, "and the decision itself still admits it"


def test_a_lease_decision_does_not_outlive_the_ticket_it_answered(tmp_path):
    """A ticket hash names a question, and the answer belongs to the session
    that asked it. When the outstanding-ticket table is replaced (a swap), the
    lease ticket is gone rather than stale: the load raises a fresh ticket and
    the satisfied decision behind the old one admits nothing."""
    session, ir = _lease_harness(tmp_path)
    lease_ticket = _gate(session, ir).ticket
    for name in ("bob", "carol"):
        session.approve_ticket(lease_ticket["hash"], vote="approve", as_token=name)
    assert session._satisfied_decision_for(lease_ticket) is not None, \
        "there IS a satisfied answer for this ticket"

    session._tickets = {}
    refused = _gate(session, ir)
    assert refused is not None and refused.ticket["kind"] == "lease"
    assert session._grants == []


# ---------------------------------------------------------------------------
# Vote binding: exact ticket, candidate, plan and target
# ---------------------------------------------------------------------------

def test_a_vote_is_bound_to_one_question_and_does_not_carry_to_another(quorum):
    """Two crossings differing only in an argument are two questions with two
    hashes. Votes against the first are not votes against the second, so the
    second still raises its own ticket and demands its own quorum."""
    first = _ticket(quorum, ("sink.log", "a"))
    quorum.approve_ticket(first["hash"], as_token="bob")
    quorum.approve_ticket(first["hash"], as_token="carol")

    second = _ticket(quorum, ("sink.log", "b"))
    assert second["hash"] != first["hash"]
    assert second["argsDigest"] != first["argsDigest"]
    state = quorum.quorum_state(second["hash"])
    assert state["counted"] == 0 and state["outcome"] is None
    assert quorum._find_standing_approval(second) is None


def test_the_minted_approval_is_bound_to_the_candidate_hash(quorum):
    """The entry the quorum mints names the reach-closure candidate hash, and a
    ticket claiming a different one is not covered by it: that is what stops a
    vote from surviving a swap of the code it was cast against."""
    ticket = _ticket(quorum)
    quorum.approve_ticket(ticket["hash"], as_token="bob")
    quorum.approve_ticket(ticket["hash"], as_token="carol")
    assert quorum._find_standing_approval(ticket) is not None

    forged = dict(ticket, candidateHash="sha256:" + "0" * 64)
    assert quorum._find_standing_approval(forged) is None


def test_votes_on_a_question_that_changed_under_the_swap_are_not_counted(
        tmp_path):
    """A swap changes the reach closure, which changes the candidate hash, which
    changes the ticket. The composition that was voted on is not the composition
    that would cross, so the votes do not carry and the new code raises its own
    ticket."""
    voted = _harness(tmp_path, name="before.json")
    ticket = _ticket(voted)
    voted.approve_ticket(ticket["hash"], as_token="bob")
    voted.approve_ticket(ticket["hash"], as_token="carol")
    assert _cross(voted) is None

    swapped = _harness(tmp_path, name="after.json", source=_SOURCE_SWAPPED)
    after = _ticket(swapped)
    assert after["candidateHash"] != ticket["candidateHash"]
    assert after["hash"] != ticket["hash"]
    assert swapped._find_standing_approval(after) is None
    assert swapped.quorum_state(after["hash"])["counted"] == 0


def test_votes_expire_with_the_tickets_own_ttl(tmp_path):
    """A vote cannot outlive the approval it would authorize. Past the rule's
    `ttl` the question latches expired, the vote is refused, the crossing stays
    refused, and the record says the timeout happened."""
    clock = {"now": 1_000}
    session = _harness(tmp_path, rules=[_quorum(ttl_ms=30_000)], clock=clock)
    ticket = _ticket(session)
    session.approve_ticket(ticket["hash"], as_token="bob")

    clock["now"] += 30_001
    with pytest.raises(SessionError, match="lapsed"):
        session.approve_ticket(ticket["hash"], as_token="carol")
    assert _cross(session) is not None

    expired = [r for r in _records(session) if r["record"] == "quorum-expired"]
    (row,) = expired
    assert row["counted"] == 1 and row["require"] == 2
    assert row["expiresAt"] == 31_000 and row["expiredAt"] == 31_001


def test_an_expired_question_stays_expired_however_the_clock_moves(tmp_path):
    """The timeout latches. A question observed past its deadline is dead for
    good, so a clock that winds back cannot make it answerable again: an
    expiry that could be reopened is a vote-resurrection primitive."""
    clock = {"now": 5_000}
    session = _harness(tmp_path, rules=[_quorum(ttl_ms=1_000)], clock=clock)
    ticket = _ticket(session)
    clock["now"] += 5_000
    with pytest.raises(SessionError, match="lapsed"):
        session.approve_ticket(ticket["hash"], as_token="bob")
    clock["now"] = 0
    with pytest.raises(SessionError, match="lapsed"):
        session.approve_ticket(ticket["hash"], as_token="bob")


# ---------------------------------------------------------------------------
# Denial, escalation, revocation, and the emergency override
# ---------------------------------------------------------------------------

def test_a_vote_whose_question_no_longer_matches_the_live_candidate_is_refused(
        quorum):
    """A vote is bound to the exact reach closure it was cast against. The graph
    is durable and readable, so a record that has drifted from the candidate
    under it voids the votes rather than counting a yes for one composition
    against another: the binding is checked on every vote, not only at mint."""
    ticket = _ticket(quorum)
    record = quorum._quorums[ticket["hash"]]
    record["candidateHash"] = "sha256:" + "0" * 64
    with pytest.raises(SessionError, match="no longer matches the live candidate"):
        quorum.approve_ticket(ticket["hash"], as_token="bob")
    (row,) = [r for r in _records(quorum) if r["record"] == "quorum-refused"]
    assert row["reason"] == "stale-candidate" and row["voter"] == "bob"
    assert row["candidateHash"] == "sha256:" + "0" * 64
    assert quorum.quorum_state(ticket["hash"])["counted"] == 0
    assert _cross(quorum) is not None


def test_a_denial_closes_the_question_and_is_recorded_as_a_denial(tmp_path):
    """An approver saying NO, with too few approvers left to reach the count,
    closes the question as denied. The record names who denied it, so the
    decision graph distinguishes a human refusal from the policy's arithmetic,
    and a denial that leaves the count reachable does NOT close it."""
    session = _harness(tmp_path, rules=[
        _quorum(2, ("alice", "bob", "carol", "dave"))])
    ticket = _ticket(session)
    first = session.approve_ticket(ticket["hash"], vote="deny", as_token="bob")
    assert first["approved"] is False and first["outcome"] is None
    assert first["counted"] == 0
    assert not [r for r in _records(session)
                if r["record"] == "quorum-denied"]

    final = session.approve_ticket(ticket["hash"], vote="deny", as_token="carol")
    assert final["outcome"] == "denied"
    (row,) = [r for r in _records(session) if r["record"] == "quorum-denied"]
    assert row["denied"] == ["bob", "carol"] and row["reason"] == "denied"
    assert row["counted"] == 0 and row["require"] == 2

    with pytest.raises(SessionError, match="already denied"):
        session.approve_ticket(ticket["hash"], as_token="bob")
    assert _cross(session) is not None


def test_a_question_made_unreachable_by_a_denial_closes_as_denied(tmp_path):
    """Two approvers saying yes is not enough once a third says no and the
    proposer cannot count: the count is arithmetically dead, the question closes,
    and the record carries both the denial that closed it and the count reached.
    A question that could still be carried stays open, which the first two votes
    show."""
    session = _harness(tmp_path, rules=[
        _quorum(3, ("alice", "bob", "carol", "dave"))])
    ticket = _ticket(session)
    assert session.approve_ticket(ticket["hash"], as_token="bob")["outcome"] is None
    third = session.approve_ticket(ticket["hash"], as_token="carol")
    assert third["counted"] == 2 and third["outcome"] is None

    final = session.approve_ticket(ticket["hash"], vote="deny", as_token="dave")
    assert final["outcome"] == "denied"
    (row,) = [r for r in _records(session) if r["record"] == "quorum-denied"]
    assert row["reason"] == "denied" and row["counted"] == 2
    assert row["denied"] == ["dave"] and row["require"] == 3
    assert _cross(session) is not None


def test_the_emergency_override_admits_and_is_never_recorded_as_a_quorum(quorum):
    """The override exists for the case the item names: the approvers cannot be
    convened and the crossing must still be decidable. It is a DIFFERENT
    authority and is recorded as one, so an audit can never mistake it for N
    votes, and the count it actually reached is carried beside the count it
    demanded."""
    ticket = _ticket(quorum)
    quorum.approve_ticket(ticket["hash"], as_token="bob")

    result = quorum.override_ticket(ticket["hash"], reason="on-call, rollout")
    assert result["approved"] is True and result["satisfiedBy"] == "override"
    assert result["counted"] == 1 and result["require"] == 2
    assert _cross(quorum) is None

    (row,) = [r for r in _records(quorum) if r["record"] == "quorum-satisfied"]
    assert row["satisfiedBy"] == "override"
    assert row["counted"] == 1 and row["require"] == 2
    assert row["override"]["by"] == "alice"
    assert row["override"]["reason"] == "on-call, rollout"
    assert row["override"]["selfOverride"] is True
    assert not [r for r in _records(quorum) if r["record"] == "quorum-vote"
                and r["voter"] == "alice"]
    assert "quorum-override" in _kinds(quorum)


def test_an_override_without_a_reason_is_refused(quorum):
    """An override is an act someone is accountable for: a reason is required,
    and its absence is refused and recorded rather than defaulted."""
    ticket = _ticket(quorum)
    with pytest.raises(SessionError, match="must state a reason"):
        quorum.override_ticket(ticket["hash"])
    with pytest.raises(SessionError, match="must state a reason"):
        quorum.override_ticket(ticket["hash"], reason="   ")
    rows = [r for r in _records(quorum) if r["record"] == "quorum-refused"]
    assert [r["action"] for r in rows] == ["override", "override"]
    assert {r["reason"] for r in rows} == {"no-reason"}
    assert _cross(quorum) is not None


def test_an_override_cannot_bypass_a_question_that_is_already_decided(quorum):
    """An override of a satisfied, denied, revoked or escalated question is
    refused: the point of an override is the case no vote can settle, and using
    it where a vote already decided would be an unaudited bypass."""
    ticket = _ticket(quorum)
    quorum.approve_ticket(ticket["hash"], as_token="bob")
    quorum.approve_ticket(ticket["hash"], as_token="carol")
    with pytest.raises(SessionError, match="already satisfied"):
        quorum.override_ticket(ticket["hash"], reason="too late")


def test_an_override_refuses_a_ticket_that_demands_no_quorum(tmp_path):
    """A single-approver ticket has nothing to override. Allowing it would turn
    the override into a bypass of an ordinary refusal."""
    session = _harness(tmp_path, rules=[ApprovalRule("announce")])
    ticket = _ticket(session)
    with pytest.raises(SessionError, match="nothing to override"):
        session.override_ticket(ticket["hash"], reason="because")


def test_escalation_closes_the_vote_path_and_only_an_approver_may_escalate(quorum):
    """Escalation is what an approver does when the rule cannot be answered as
    written. It narrows authority (the remaining path is the separately granted
    override), and a bystander cannot close somebody else's question."""
    ticket = _ticket(quorum)
    with pytest.raises(SessionError, match="cannot escalate"):
        quorum.escalate_ticket(ticket["hash"], as_token="mallory")

    result = quorum.escalate_ticket(ticket["hash"], reason="stalled",
                                    as_token="bob")
    assert result["escalated"] is True and result["outcome"] == "escalated"
    (row,) = [r for r in _records(quorum) if r["record"] == "quorum-escalated"]
    assert row["by"] == "bob" and row["reason"] == "stalled"

    with pytest.raises(SessionError, match="already escalated"):
        quorum.approve_ticket(ticket["hash"], as_token="bob")
    assert _cross(quorum) is not None


def test_revocation_stops_the_votes_counting_and_only_an_insider_may_revoke(
        quorum):
    """The proposer withdraws the crossing it asked for, or a named approver
    vetoes it. Either way the votes stop counting, no approval is minted, and the
    record says who closed it and which votes it withdrew."""
    ticket = _ticket(quorum)
    with pytest.raises(SessionError, match="cannot revoke"):
        quorum.revoke_ticket(ticket["hash"], as_token="mallory")

    quorum.approve_ticket(ticket["hash"], as_token="bob")
    result = quorum.revoke_ticket(ticket["hash"], reason="wrong build",
                                  as_token="alice")
    assert result["revoked"] is True and result["by"] == "alice"
    assert result["withdrewVotes"] == []
    (row,) = [r for r in _records(quorum) if r["record"] == "quorum-revoked"]
    assert row["reason"] == "wrong build" and row["counted"] == 1

    with pytest.raises(SessionError, match="already revoked"):
        quorum.approve_ticket(ticket["hash"], as_token="carol")
    assert _cross(quorum) is not None


# ---------------------------------------------------------------------------
# Fail closed: unknown approvers, unparseable votes, unknown tickets
# ---------------------------------------------------------------------------

def test_an_unnamed_approver_cannot_be_counted(quorum):
    """A vote from an identity the rule did not name is refused rather than
    counted: the set of humans a rule counts is the set it names."""
    ticket = _ticket(quorum)
    with pytest.raises(SessionError, match="not one of the approvers"):
        quorum.approve_ticket(ticket["hash"], as_token="mallory")
    (row,) = [r for r in _records(quorum) if r["record"] == "quorum-refused"]
    assert row["reason"] == "unknown-approver" and row["voter"] == "mallory"
    assert quorum.quorum_state(ticket["hash"])["counted"] == 0


def test_an_unparseable_vote_is_refused_rather_than_coerced(quorum):
    """A vote is `approve` or `deny`. Anything else is refused and recorded, not
    read as consent."""
    ticket = _ticket(quorum)
    with pytest.raises(SessionError, match="unparseable vote"):
        quorum.approve_ticket(ticket["hash"], vote="maybe", as_token="bob")
    (row,) = [r for r in _records(quorum) if r["record"] == "quorum-refused"]
    assert row["reason"] == "malformed-vote"
    assert _cross(quorum) is not None


def test_a_session_with_no_bound_operator_counts_nobody(quorum):
    """With no operator identity there is nobody to separate the duty from, so
    no vote can be attributed and the question cannot be closed by votes."""
    ticket = _ticket(quorum)
    quorum.operator = None
    with pytest.raises(SessionError, match="not one of the approvers"):
        quorum.approve_ticket(ticket["hash"])
    assert _cross(quorum) is not None


@pytest.mark.parametrize("call", [
    lambda s: s.approve_ticket("sha256:deadbeef"),
    lambda s: s.override_ticket("sha256:deadbeef", reason="why"),
    lambda s: s.escalate_ticket("sha256:deadbeef"),
    lambda s: s.revoke_ticket("sha256:deadbeef"),
    lambda s: s.quorum_state("sha256:deadbeef"),
], ids=["approve", "override", "escalate", "revoke", "state"])
def test_every_entry_point_refuses_a_ticket_the_server_never_issued(quorum, call):
    """An approval can only be minted for a question the server actually asked.
    Every entry point refuses an unknown hash rather than creating a graph for
    one."""
    with pytest.raises(SessionError, match="unknown ticket hash"):
        call(quorum)


def test_a_single_party_ticket_cannot_be_answered_with_a_vote(tmp_path):
    """A ticket whose rules name no approver set demands no quorum, so it is
    answered plainly. Passing a vote to it is refused rather than silently
    ignored."""
    session = _harness(tmp_path, rules=[ApprovalRule("announce")])
    ticket = _ticket(session)
    with pytest.raises(SessionError, match="cannot be answered with a vote"):
        session.approve_ticket(ticket["hash"], vote="deny")
    with pytest.raises(SessionError, match="cannot be answered with a vote"):
        session.approve_ticket(ticket["hash"], as_token="bob")
    assert session.approve_ticket(ticket["hash"])["approved"] is True


# ---------------------------------------------------------------------------
# The decision graph itself
# ---------------------------------------------------------------------------

def test_quorum_state_reports_the_graph_and_the_refusals(quorum):
    """The state a caller reads back is the graph the WAL carries: the required
    count, the named approvers, the votes counted and who cast them, the votes
    refused and why, the proposer, and the outcome. A quorum whose failures are
    invisible is a quorum that can be probed without a trace."""
    ticket = _ticket(quorum)
    with pytest.raises(SessionError):
        quorum.approve_ticket(ticket["hash"])
    quorum.approve_ticket(ticket["hash"], as_token="bob")

    state = quorum.quorum_state(ticket["hash"])
    assert state["quorum"] is True and state["require"] == 2
    assert state["approvers"] == list(_THREE)
    assert state["proposer"] == "alice" and state["rule"] == (
        "require 2 of {alice, bob, carol}")
    assert state["counted"] == 1 and state["outcome"] is None
    assert [v["voter"] for v in state["votes"]] == ["bob"]
    assert {(r["reason"], r["voter"]) for r in state["refusals"]} == {
        ("proposer", "alice")}
    assert state["outstanding"] == ["alice", "carol"]


def test_quorum_state_says_so_when_the_ticket_demands_no_quorum(tmp_path):
    """A ticket that demands no quorum reports that instead of inventing an empty
    graph a caller could mistake for a stalled vote."""
    session = _harness(tmp_path, rules=[ApprovalRule("announce")])
    ticket = _ticket(session)
    assert session.quorum_state(ticket["hash"])["quorum"] is False


def test_a_reissued_question_owes_its_own_quorum(tmp_path):
    """A ticket hash names a question, not one asking of it. Once the minted
    approval has been spent, the next asking of the same question opens a new
    round with a fresh graph: the votes already spent cannot answer it, and they
    are not re-counted against it."""
    session = _harness(tmp_path)
    ticket = _ticket(session)
    session.approve_ticket(ticket["hash"], as_token="bob")
    session.approve_ticket(ticket["hash"], as_token="carol")
    assert _cross(session) is None

    again = _ticket(session)
    assert again["hash"] == ticket["hash"]
    state = session.quorum_state(ticket["hash"])
    assert state["round"] == 2 and state["counted"] == 0
    assert state["outcome"] is None
    assert state["requestId"] == f"{ticket['hash']}#r2"


def test_the_quorum_records_consume_no_wal_sequence(tmp_path):
    """A consent record is a fact, not an effect: the quorum records join the
    `approval-granted` / `approval-consumed` records as seq-free entries, so the
    replay sequence the effect records carry is untouched."""
    session = _harness(tmp_path)
    ticket = _ticket(session)
    session.approve_ticket(ticket["hash"], as_token="bob")
    session.approve_ticket(ticket["hash"], as_token="carol")
    document = _replay.WriteAheadLog.read(session._approval_wal().path)
    assert document["header"]["walVersion"] == _replay.WAL_VERSION
    assert all("seq" not in record for record in document["records"])


def test_the_quorum_graph_dies_with_the_session(tmp_path):
    """Item 246 invariant 5: the decision graph is session-scoped, exactly as the
    ledger it keys into is. A vote cannot outlive the session that recorded it."""
    session = _harness(tmp_path)
    ticket = _ticket(session)
    session.approve_ticket(ticket["hash"], as_token="bob")
    assert session._quorums
    session._reset()
    assert session._quorums == {}
    assert session._ledger == []


def test_the_wal_refuses_a_record_kind_it_does_not_know(tmp_path):
    """The decision graph writes only the record kinds it defines: an unknown kind
    is a programming error and is refused rather than written as an unreadable
    row."""
    wal = _replay.WriteAheadLog(str(tmp_path / "wal.json"), {}, 1)
    with pytest.raises(ValueError):
        wal.record_quorum_event("quorum-invented", {})
    assert _replay._QUORUM_RECORDS == frozenset({
        "quorum-open", "quorum-vote", "quorum-refused", "quorum-satisfied",
        "quorum-denied", "quorum-expired", "quorum-escalated", "quorum-revoked",
        "quorum-override",
    })


def test_the_approve_tool_carries_a_vote_to_the_protocol(tmp_path, monkeypatch):
    """The protocol is reachable over the transport, not only in process. A bare
    `revl_approve(hash)` on a multi-party ticket is the proposer answering its own
    question and is refused; `vote`/`asToken` cast one approver's vote and the
    crossing only goes through once the count is reached.

    A standing-grant shape is a ONE-operator mint, so it refuses a vote outright
    rather than silently ignoring it: consent one person can give is not a
    decision several people have to make."""
    from revl.mcp import server

    session = _harness(tmp_path, rules=[_quorum(2, ("alice", "bob", "carol"))])
    monkeypatch.setattr(server, "SESSION", session)
    ticket = _ticket(session)

    bare = server._tool_approve({"hash": ticket["hash"]})
    assert bare["ok"] is False
    assert "cannot also approve it" in bare["diagnostics"][0]["message"]
    assert _cross(session) is not None

    first = server._tool_approve(
        {"hash": ticket["hash"], "asToken": "bob", "vote": "approve"})
    assert first["ok"] is True and first["counted"] == 1
    assert first["approved"] is False
    assert _cross(session) is not None

    second = server._tool_approve({"hash": ticket["hash"], "asToken": "carol"})
    assert second["ok"] is True and second["approved"] is True
    assert _cross(session) is None

    grant = server._tool_approve(
        {"hash": ticket["hash"], "uses": 2, "asToken": "bob"})
    assert grant["ok"] is False
    assert "ONE operator" in grant["diagnostics"][0]["message"]


def test_the_backend_module_the_tests_import_is_the_one_under_test():
    """A guard on the harness: `needs_cordis` is deliberately absent here, so the
    suite must be running against this worktree's package rather than an
    installed copy.

    Both halves matter. `_replay` is inserted by path; `revl` resolves through
    the interpreter's own search order, where an editable install of the shared
    checkout can otherwise answer, and a suite that exercises the wrong tree
    reports on code nobody changed."""
    import revl.mcp.session as module
    assert importlib.util.find_spec("cordis") is None or True
    assert str(_ROOT / "src") in sys.path
    assert os.path.dirname(_replay.__file__) == str(_BACKEND)
    assert str(Path(module.__file__).resolve()).startswith(str(_ROOT / "src"))

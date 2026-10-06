"""A clause that can mark a capability NEVER-STANDING —issue #1982.

Item 246's Decision 3 keeps exactly two ways to consume a class-(c) crossing: a
per-call yes (`revl_approve(hash)`), which covers ONE call, and a STANDING yes
that covers a series. The standing shapes arrived later —the item-344
session-scoped grant (`revl_approve` with `capability` + `uses`/`ttlMs`) and the
item-251 distilled `AutoApproveRule` —and each records one operator yes and then
covers every later crossing until `uses` runs out or the TTL lapses. The policy
could gate WHO says yes and how many approvers a crossing needs, and it could
bound the crossing in the OTHER direction (`approvals require bounded
crossings`), but nothing could say "not this capability, not as a standing
thing".

So the policy was consulted for the per-call floor and not for the widening, and
a deployment that considers a capability never-standing (a mail send, a shell
escape, a payment) could not express it: one mint covered an unbounded number of
crossings, fail-open consent the policy could not withdraw.

`NeverStandingRule` is that clause, and the tests below are written against the
three ways the fix could have gone wrong:

  * REFUSING TOO MUCH. A never-standing capability stays USABLE. The crossing
    still prompts and `revl_approve(hash=…)` still answers it; only the two
    STANDING shapes are refused, and an UNMARKED capability keeps minting
    exactly as before (probe (b2) and the over-refusal control).
  * REFUSING TOO LITTLE, on the other verb. A distilled rule IS standing
    auto-approval reached through a different verb, so a clause enforced only on
    `revl_approve(capability=…)` would leave `apply_distillation` as the way
    around it - and `revl_distillation_offers` would invite the operator to
    review a rule that cannot be applied (probe (c)).
  * READING AN UNKNOWN AS ALLOWED. The predicate reads every capability slot the
    ticket carries rather than the spelling the caller passed, and a spelling
    `cap_order` cannot parse is compared as itself rather than waved through.

The session here is cordis-free (`_StubClassMap`, the shape
`tests/test_1062_mint_bound.py` uses): the gate under test is revl's own policy
and approval layer, and nothing below needs a runtime.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from revl.mcp import session as session_mod  # noqa: E402
from revl.mcp.operator import parse_profile  # noqa: E402
from revl.mcp.session import Session, SessionError  # noqa: E402
from revl.policy import PolicyError, parse_policy  # noqa: E402

_ANNOUNCE = "announce"
_MAIL = "mail.send"
_SHELL = "shell.run"

#: The clause, in the shape the issue suggests.
NEVER = "capability announce may never be granted standing"

#: A policy that requires per-call approval - and, before this clause, could not
#: say anything about the standing form of the same yes.
REQUIRES = "capability announce requires approval"

#: An operator that may approve everything and mint what it likes, so nothing
#: below is the item-470/issue-#1062 `may mint` declaration refusing instead.
UNBOUNDED_OPS = """
operator ops may approve on *
operator ops may mint * uses * ttl *
"""


class _StubClassMap:
    """The live class map's two readers, for a session with no composition."""

    ir = {"components": [{"name": "Agent"}]}

    def crossings_for_capability(self, capability):
        return [{"component": "Agent", "candidateHash": "h0",
                 "capabilities": [capability]}]

    def static_taint(self, component):
        # a clean crossing, the contract `ClassMap.static_taint` has: an empty
        # set (NOT None, which means "no honest source" and floors the
        # admission set to every taint-fold origin, fail-closed).
        return frozenset()


def _session(policy: str = NEVER, profile: str = UNBOUNDED_OPS) -> Session:
    session = Session()
    session._class_map = _StubClassMap()
    if policy is not None:
        session.sandbox = parse_policy(policy, "p.policy")
    if profile is not None:
        registry = parse_profile(profile)
        session.operator_registry = registry
        session.operator = registry.get("ops")
    return session


def _ticket(session, caps, component="Agent"):
    """An outstanding class-(c) ticket, the shape `_issue_ticket` stores."""
    body = {"hash": "h1", "component": component, "candidateHash": "h0",
            "capabilities": list(caps), "classCCapabilities": list(caps),
            "realm": "", "taintOrigins": []}
    session._tickets["h1"] = body
    return "h1"


# ---------------------------------------------------------------------------
# (a) the grammar: the clause is ACCEPTED, and the probe's other candidates are
#     unchanged
# ---------------------------------------------------------------------------

def test_the_clause_is_accepted_and_its_neighbours_are_unchanged():
    """Probe (a) of the issue, verbatim. The first candidate is the clause; the
    other two still refuse as unrecognised lines (so this is a GRAMMAR
    ADDITION, not a loosening of the parser), and `requires approval` still
    parses as it always did."""
    verdicts = {}
    for cand in ("capability mail.send may never be granted standing",
                 "standing grants refused for mail.send",
                 "capability mail.send never standing",
                 "capability mail.send requires approval"):
        try:
            parse_policy(cand, "p.policy")
            verdicts[cand] = "ACCEPTED"
        except PolicyError as exc:
            verdicts[cand] = f"REFUSED: {str(exc).splitlines()[0]}"

    assert verdicts["capability mail.send may never be granted standing"] \
        == "ACCEPTED"
    for still_refused in ("standing grants refused for mail.send",
                          "capability mail.send never standing"):
        assert verdicts[still_refused].startswith("REFUSED")
        assert "unrecognised policy line" in verdicts[still_refused]
    assert verdicts["capability mail.send requires approval"] == "ACCEPTED"


def test_the_rule_is_a_rule_and_not_a_deny_list():
    """It marks a capability never-STANDING. It does not mark it unreachable
    and it does not mark it unapprovable: the covering `requires approval` rule
    is untouched, and a crossing of the same capability is still a crossing."""
    pol = parse_policy(NEVER + "\n" + REQUIRES, "p.policy")
    assert [r.pattern for r in pol.never_standing_rules] == [_ANNOUNCE]
    assert pol.approval_rule_for(_ANNOUNCE) is not None
    assert not pol.is_empty()
    assert pol.never_standing_for(_ANNOUNCE).to_dsl() == NEVER


def test_a_glob_marks_the_cone_and_leaves_everything_else_alone():
    """The object dimension is a glob over capability tokens, the same closed
    vocabulary a `may reach` rule and an `ApprovalRule` are held to."""
    pol = parse_policy("capability shell.* may never be granted standing",
                       "p.policy")
    assert pol.never_standing_for(_SHELL) is not None
    assert pol.never_standing_for("shell.exec") is not None
    assert pol.never_standing_for(_ANNOUNCE) is None
    assert pol.never_standing_for(_MAIL) is None


def test_the_json_form_states_the_same_grammar():
    """Two parsers, one grammar. A JSON policy that admitted a clause the DSL
    refuses would be the shape an author reaches for when the DSL says no."""
    doc = ('{"neverStanding": [{"capability": "mail.send"}, "shell.*"]}')
    pol = parse_policy(doc, "p.json")
    assert [r.pattern for r in pol.never_standing_rules] \
        == [_MAIL, "shell.*"]
    assert pol.never_standing_for(_MAIL) is not None
    # and an entry that names no capability is refused rather than read as
    # "nothing to forbid"
    with pytest.raises(PolicyError):
        parse_policy('{"neverStanding": [{"ttl": "1m"}]}', "p.json")


def test_a_line_that_does_not_name_one_capability_is_refused():
    """The clause is not a free-form sentence: a line that reaches the phrase
    without naming exactly one glob is refused, and a glob outside the
    capability vocabulary is refused at parse time rather than never matching."""
    for bad in ("may never be granted standing",
                "capability may never be granted standing",
                "capability mail.send shell.run may never be granted standing",
                "realm prod may never be granted standing"):
        with pytest.raises(PolicyError):
            parse_policy(bad, "p.policy")
    with pytest.raises(PolicyError):
        parse_policy("capability <<>> may never be granted standing",
                     "p.policy")


def test_a_policy_without_the_clause_is_empty_of_it():
    """MIGRATION. Every policy written before this clause parses to the same
    object it did before, with an empty tuple and nothing refused."""
    pol = parse_policy(REQUIRES, "p.policy")
    assert pol.never_standing_rules == ()
    assert pol.never_standing_for(_ANNOUNCE) is None


# ---------------------------------------------------------------------------
# (b) the mint: a standing grant for a never-standing capability is REFUSED
# ---------------------------------------------------------------------------

def test_a_standing_grant_for_a_never_standing_capability_is_refused():
    """Probe (b) of the issue, inverted. FAILS ON MAIN, where this mint lands
    with `remainingUses: 1000000` and then covers that many crossings."""
    session = _session()
    with pytest.raises(SessionError) as exc:
        session.mint_standing_grant(capability=_ANNOUNCE, uses=10 ** 6)
    message = str(exc.value)
    # the diagnostic names the capability and quotes the policy line, exactly
    # as the issue asks
    assert f"`{_ANNOUNCE}`" in message
    assert NEVER in message
    assert "issue #1982" in message
    # and nothing was minted: a refused mint leaves no residue
    assert session._grants == []


def test_a_ttl_shaped_mint_is_refused_too():
    """The issue calls this out: `ttl` bounds the grant's LIFETIME, not its
    shape, so a large TTL is the same unbounded grant by another axis."""
    session = _session()
    with pytest.raises(SessionError):
        session.mint_standing_grant(capability=_ANNOUNCE, ttl_ms=10 ** 9)
    assert session._grants == []


def test_a_mint_from_an_outstanding_ticket_is_refused_too():
    """The other way of naming what is granted. A ticket hash carries no
    capability in its arguments, which is why the refusal is enforced at
    `_mint_grant` (where the ticket's own spellings are resolved) rather than
    at the verb gate - and why a mint that names the capability ONLY in the
    ticket is still refused: the context is resolved, never taken from the
    request."""
    session = _session()
    with pytest.raises(SessionError) as exc:
        session.mint_standing_grant(
            ticket_hash=_ticket(session, [_ANNOUNCE]), uses=1, ttl_ms=1000)
    assert NEVER in str(exc.value)
    assert session._grants == []


def test_a_resource_scoped_spelling_is_caught_by_a_bare_token_clause():
    """A ticket renders, and a grant stores, the item-294 resource-scoped
    spelling (`shell.run(host="x")`). A clause written over the bare token must
    not be silently inert against every parametrized spelling of the same
    boundary - that would be a clause that refuses nothing."""
    session = _session("capability shell.* may never be granted standing")
    with pytest.raises(SessionError) as exc:
        session.mint_standing_grant(capability='shell.run(host="h1")',
                                    uses=1, ttl_ms=1000)
    assert session._grants == []
    assert "may never be granted standing" in str(exc.value)


# ---------------------------------------------------------------------------
# (b2) the per-call floor is UNTOUCHED - the capability stays usable
# ---------------------------------------------------------------------------

def test_a_per_call_approval_for_the_same_capability_still_succeeds():
    """The whole point of the fix, in the direction it must not overshoot. A
    never-standing capability is not unapprovable: one yes for one exact call is
    the item-246 Decision 3 floor, and it still answers."""
    session = _session(NEVER + "\n" + REQUIRES)
    ticket_hash = _ticket(session, [_ANNOUNCE])
    out = session.approve_ticket(ticket_hash)
    assert out == {"approved": True, "hash": "h1", "component": "Agent",
                   "key": None, "method": None, "kind": None,
                   "candidateHash": "h0"}
    # a single-use entry, not a grant: the standing shape is what is forbidden
    assert session._grants == []
    assert session._ledger_entry_for_ticket(ticket_hash) is not None


def test_an_unmarked_capability_can_still_be_granted_standing():
    """NO OVER-REFUSAL. The control that keeps the clause from being a
    blanket: with `announce` marked, the same mint over an unmarked capability
    lands exactly as it did before this clause existed."""
    session = _session()
    assert session.mint_standing_grant(
        capability=_MAIL, uses=10 ** 6)["granted"] is True
    assert session._grants[-1]["capability"] == _MAIL
    assert session._grants[-1]["remainingUses"] == 10 ** 6


def test_a_policy_without_the_clause_mints_exactly_as_before():
    """MIGRATION at the session, and the half that keeps this adoptable."""
    session = _session(REQUIRES)
    assert session.mint_standing_grant(
        capability=_ANNOUNCE, uses=10 ** 6)["granted"] is True


def test_a_session_with_no_policy_at_all_is_unchanged():
    session = _session(None)
    assert session.mint_standing_grant(
        capability=_ANNOUNCE, uses=10 ** 6)["granted"] is True


# ---------------------------------------------------------------------------
# (c) the other verb: a distilled auto-approve rule cannot go around it
# ---------------------------------------------------------------------------

def _offer(caps, uses=None, ttl_ms=None, component="Agent"):
    """The distillation offer `apply_distillation` folds, reduced to the fields
    it reads. The rule is the real `policy.AutoApproveRule`."""
    from revl.policy import AutoApproveRule
    rule = AutoApproveRule(component=component, caps=tuple(caps),
                           uses=uses, ttl_ms=ttl_ms)
    blast = types.SimpleNamespace(not_covered=(), covered=1, total=1)
    return types.SimpleNamespace(rule=rule, blast=blast, operator="ops",
                                 sessions=("s1",), grant_count=3,
                                 rule_text=rule.to_dsl())


def _with_offer(session, offer):
    session._offer_by_id = lambda offer_id: offer
    return session


def test_a_distilled_rule_for_a_never_standing_capability_is_refused():
    """Probe (c). A distilled rule IS standing auto-approval reached by a
    different verb, so a clause enforced only on the mint would leave this as
    the way around it. FAILS ON MAIN, where the rule installs."""
    session = _with_offer(_session(), _offer([_ANNOUNCE], uses=1, ttl_ms=1000))
    with pytest.raises(SessionError) as exc:
        session.apply_distillation("offer-1")
    message = str(exc.value)
    assert f"`{_ANNOUNCE}`" in message and NEVER in message
    assert not getattr(session.sandbox, "auto_approve_rules", ())


def test_the_offer_is_never_made_for_such_a_capability():
    """The fail-closed end the issue names first: the operator is not invited to
    review a rule that cannot be applied. The offer is withheld and the reason
    is reported in the same shape `distill`'s own refusals use."""
    session = _session()
    session._approval_records = [
        {"record": "approval-granted", "component": "Agent", "session": f"s{i % 3}",
         "operator": "ops", "realm": "", "classCCapabilities": [_ANNOUNCE],
         "taintOrigins": []}
        for i in range(6)]
    out = session.distillation_offers()
    assert out["offers"] == []
    assert [r["reason"] for r in out["refusals"]] == ["never-standing"]
    assert out["refusals"][0]["token"] == _ANNOUNCE


def test_a_distilled_rule_over_an_unmarked_capability_still_applies():
    """NO OVER-REFUSAL on this route either."""
    session = _with_offer(_session(), _offer([_MAIL], uses=5, ttl_ms=1000))
    assert session.apply_distillation("offer-1")["applied"] is True
    assert len(session.sandbox.auto_approve_rules) == 1


def test_a_grant_minted_before_the_clause_stops_covering():
    """The EVALUATION half, and why the guard is not mint-only. A grant that
    already exists - minted under a policy with no clause, or carried across a
    `dataclasses.replace` of the policy - stops covering the moment the clause
    is in force, instead of outliving the policy that forbids it. Without
    `_find_standing_grant`'s check, binding the clause would leave the grant
    the policy was written to forbid live for its whole `uses` budget."""
    session = _session(REQUIRES)
    ticket_hash = _ticket(session, [_ANNOUNCE])
    session.mint_standing_grant(ticket_hash=ticket_hash, uses=5, ttl_ms=1000)
    ticket = session._tickets[ticket_hash]
    assert session._find_standing_grant(ticket) is not None
    session.sandbox = parse_policy(REQUIRES + "\n" + NEVER, "p.policy")
    assert session._find_standing_grant(ticket) is None


def test_a_hand_written_auto_approve_rule_is_withheld_too():
    """Where the clause meets a STANDING auto-approve in the SAME policy the
    narrower statement wins. A hand-written `may auto-approve` rule never
    reaches `apply_distillation`, so a guard living only there would leave it as
    the standing yes the clause was written to forbid; `_auto_rule_covers` is
    the one predicate both the hand-written and the distilled rule are read
    through. The control below is the same policy WITHOUT the clause."""
    text = ("component Agent may auto-approve announce ttl 1000ms uses 10\n")
    marked = _session(REQUIRES + "\n" + NEVER + "\n" + text)
    marked._install_auto_approve_rules()
    assert marked._find_auto_approve(marked._tickets[_ticket(marked,
                                                             [_ANNOUNCE])]) is None
    control = _session(REQUIRES + "\n" + text)
    control._install_auto_approve_rules()
    assert control._find_auto_approve(
        control._tickets[_ticket(control, [_ANNOUNCE])]) is not None


def test_a_rule_that_mixes_a_never_standing_capability_is_refused():
    """A multi-capability rule is refused as a whole rather than partially
    widened: one boundary the policy forbids a standing yes over is enough."""
    session = _with_offer(_session(), _offer([_MAIL, _ANNOUNCE],
                                             uses=1, ttl_ms=1000))
    with pytest.raises(SessionError):
        session.apply_distillation("offer-1")
    assert not getattr(session.sandbox, "auto_approve_rules", ())


# ---------------------------------------------------------------------------
# the predicate itself: one function, wired beside the item-471 one
# ---------------------------------------------------------------------------

def test_the_predicate_reads_every_capability_slot_the_ticket_carries():
    """FAIL-CLOSED, stated as the wiring. `_never_standing_rules` is the
    item-1982 sibling of `_multi_party_rules` and reads the same slots the
    admission paths carry, so a ticket that names the capability only in the
    class-(c) fold is refused rather than read as unmarked."""
    session = _session()
    assert session._never_standing_rules(
        {"capabilities": [_ANNOUNCE]})[0][0] == _ANNOUNCE
    assert session._never_standing_rules(
        {"classCCapabilities": [_ANNOUNCE]})[0][0] == _ANNOUNCE
    assert session._never_standing_rules({"capabilities": [_MAIL]}) == []
    assert session._never_standing_rules({}) == []


def test_a_spelling_the_order_cannot_parse_is_compared_as_itself():
    """The `_cap_covers` direction: an unparseable spelling is not assumed to be
    inside the rule and not assumed outside it either - it matches only what it
    is written as, so the clause cannot be evaded by a spelling the order
    rejects."""
    session = _session("capability announce may never be granted standing")
    assert session._never_standing_rules(
        {"capabilities": ["announce"]}) != []
    assert session._never_standing_rules(
        {"capabilities": ["announce("]}) == []


def test_the_predicate_is_consulted_by_every_standing_gate():
    """One clause, four gates, and the failure mode this pins is the one the
    issue was: a gate that consults the policy for the per-call floor and not
    for the standing widening. Each of the four reads the predicate."""
    import inspect  # noqa: PLC0415
    for fn in (Session._mint_grant, Session._find_standing_grant,
               Session._auto_rule_covers, Session._rule_never_standing):
        src = inspect.getsource(fn)
        assert "_never_standing_rules" in src, fn.__name__
    assert hasattr(session_mod.Session, "_never_standing_rules")


# ---------------------------------------------------------------------------
# the diff surface: a policy that GAINS or LOSES the clause must not read
# `unchanged`
# ---------------------------------------------------------------------------

def test_a_policy_diff_names_the_approval_leg_when_the_clause_moves():
    """`policy_diff` previews what a policy change does to a recorded crossing
    and may never report a WIDENING as `unchanged`, so the `approval` leg has to
    carry the surface the approval gate actually reads. Losing this clause is a
    widening - one operator yes starts covering unbounded crossings - while
    `approval_rule_for` is unchanged, so the leg must move in BOTH directions
    rather than read the clause as outside it."""
    from revl.policy_diff import moved_legs
    base = parse_policy(REQUIRES, "p.policy")
    marked = parse_policy(REQUIRES + "\n" + NEVER, "p.policy")
    assert moved_legs(base, marked, "Agent", _ANNOUNCE, None) == ("approval",)
    # the widening direction is the one that matters: dropping the clause
    assert moved_legs(marked, base, "Agent", _ANNOUNCE, None) == ("approval",)
    # a clause over a capability this crossing does not reach moves nothing
    other = parse_policy(REQUIRES + "\ncapability shell.* may never be granted "
                                    "standing", "p.policy")
    assert moved_legs(base, other, "Agent", _ANNOUNCE, None) == ()

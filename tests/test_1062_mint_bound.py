"""What an operator may MINT, declared — issue #1062, item 470 stage 3.

`docs/design/470-intent-refinement.md` §4 stage 3. Item 470 stage 1 made the
class-(c) approval gate refine a crossing against what a standing grant
declared. That fixed the consumption side and made the asymmetry visible: the
gate compares a crossing carefully against a grant, while the grant itself was
minted with nothing to compare it against. `operator.Grant` is verb globs over
subject globs — an AUTHORITY, not a declared intent — so an operator holding
`approve` could mint a standing grant over any capability, with any ceiling and
any number of uses, and nothing checked the mint against anything. The authority
was bounded downstream and unbounded upstream.

`operator.MintBound` is the declaration the grant side lacked, and
`operator.mint_refusal` is the comparison. Every route to a standing grant runs
through `session._mint_grant`, which is where it is enforced.

Three things are stated where they are tested, because each of them is a
direction the fix could have got wrong:

  * FAILURE DIRECTION. A mint that cannot be SHOWN to refine a declaration is
    refused, never admitted. That includes a mint that bounds no uses (or no
    window) against a declaration that bounds one: an unbounded grant cannot be
    within a stated bound, and defaulting it to the declared number would mint
    something the operator did not ask for.
  * MIGRATION. An operator that declares no `may mint` line mints exactly what
    it minted before. Refusing every profile written before this grammar
    existed would refuse every deployment that has one. The bound is adopted
    per operator, and its first line closes that operator's whole mint surface.
  * NON-VACUITY. A mint inside the declaration still lands, on this branch and
    on the one before it, so the refusals above are the comparison refusing and
    not the dimension refusing everything.

No cordis: a mint is a decision over the session's grant store and its class
map, and neither needs a live composition (`_StubClassMap` resolves the one
capability the tests mint over, exactly as the live map would).
"""

import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from revl.mcp import operator as op  # noqa: E402
from revl.mcp.operator import (  # noqa: E402
    MintBound, Operator, ProfileError, mint_refusal, parse_profile,
)
from revl.mcp.session import Session, SessionError  # noqa: E402

_TMP = 'fs.write(path="/tmp")'
_TMP_JOB = 'fs.write(path="/tmp/job-42")'
_ETC = 'fs.write(path="/etc")'
_TMP_1MB = 'fs.write(path="/tmp", size="1MB")'
_TMP_10MB = 'fs.write(path="/tmp", size="10MB")'

#: A profile that bounds the mint: one cone, ten uses, half an hour.
BOUNDED = """
operator ops may approve on *
operator ops may mint fs.write(path="/tmp") uses 10 ttl 30m
"""

#: The same operator authority with no mint declaration at all — every profile
#: written before this grammar existed.
LEGACY = """
operator ops may approve on *
"""


class _StubClassMap:
    """The live class map's two readers, for a session with no composition.

    `crossings_for_capability` is what a proactive mint resolves through and
    `ir` is what a rule's component glob enumerates over. Both answer for one
    component, which is the shape that makes a proactive mint decidable.
    """

    ir = {"components": [{"name": "Agent"}]}

    def crossings_for_capability(self, capability):
        return [{"component": "Agent", "candidateHash": "h0",
                 "capabilities": [capability]}]


def _session(profile: str | None = BOUNDED, token: str = "ops") -> Session:
    session = Session()
    session._class_map = _StubClassMap()
    if profile is not None:
        registry = parse_profile(profile)
        session.operator_registry = registry
        session.operator = registry.get(token)
    return session


# ---------------------------------------------------------------------------
# The grammar: three axes, all three stated
# ---------------------------------------------------------------------------

def test_a_may_mint_line_declares_the_cone_the_uses_and_the_window():
    """The declaration the grant side had none of. It is not a `Grant` with
    extra fields: `may approve on payments` decides who may say yes and where,
    and `may mint` decides what may be minted when they do."""
    ops = parse_profile(BOUNDED).get("ops")
    assert len(ops.mints) == 1
    bound = ops.mints[0]
    assert bound.capability == 'fs.write(path="/tmp")'
    assert bound.uses == 10
    assert bound.ttl_ms == 30 * 60 * 1000
    assert ops.bounds_mints()
    # and the verb grants are untouched by it
    assert ops.allows("approve", frozenset({"Agent"}))[0]


def test_a_line_that_leaves_an_axis_out_is_refused():
    """An omitted axis would be a bound nobody stated, which is the thing the
    record exists to remove. Both numeric clauses are required."""
    for line in ('operator ops may mint fs.write(path="/tmp") uses 10',
                 'operator ops may mint fs.write(path="/tmp") ttl 30m',
                 'operator ops may mint fs.write(path="/tmp")'):
        with pytest.raises(ProfileError) as exc:
            parse_profile(line)
        assert "may mint" in str(exc.value)


def test_unbounded_is_written_rather_than_assumed():
    """`*` on an axis is a deployment saying the axis is unbounded. Silence is
    not, which is why the clause cannot be left out to mean the same thing."""
    ops = parse_profile("operator ops may mint * uses * ttl *").get("ops")
    assert ops.mints == (MintBound("*", None, None),)
    assert ops.bounds_mints()
    # and it admits what it says it admits
    assert mint_refusal(ops, capability=_ETC, uses=10 ** 6,
                        ttl_ms=10 ** 9) is None


def test_there_is_no_may_not_mint_rule():
    """The `may mint` lines are the WHOLE of what an operator may mint, so a
    narrower bound is written by narrowing the line. A second, deny-shaped rule
    could disagree with the first, and then one of the two would bound
    nothing."""
    with pytest.raises(ProfileError) as exc:
        parse_profile('operator ops may not mint fs.write(path="/etc") '
                      'uses 1 ttl 1m')
    assert "no `may not mint` rule" in str(exc.value)


def test_a_calls_ceiling_on_the_declaration_is_refused():
    """`calls` IS the uses axis: a mint folds `calls=N` into the grant's
    `remainingUses`. Spelling it on the cone as well would bound one quantity
    by two rules and let the weaker one win."""
    with pytest.raises(ProfileError) as exc:
        parse_profile("operator ops may mint model.complete(calls=3) "
                      "uses 10 ttl 30m")
    assert "uses N" in str(exc.value)


def test_a_declaration_that_is_not_a_capability_is_refused():
    """A `may mint` line names a point in the capability order, which is what a
    mint is compared against. It is not a glob."""
    with pytest.raises(ProfileError) as exc:
        parse_profile("operator ops may mint fs.* uses 10 ttl 30m")
    assert "is a glob" in str(exc.value)
    with pytest.raises(ProfileError):
        parse_profile("operator ops may mint fs.write(path=/tmp) uses 1 ttl 1m")


def test_the_json_profile_states_the_same_grammar():
    """Two parsers, one grammar. A JSON profile that admitted a bound the DSL
    refuses would be the shape an author reaches for when the DSL says no."""
    doc = ('{"operators": [{"token": "ops",'
           ' "grants": [{"verbs": ["approve"], "on": ["*"]}],'
           ' "mints": [{"capability": "fs.write(path=\\"/tmp\\")",'
           ' "uses": 10, "ttl": "30m"}]}]}')
    assert parse_profile(doc).get("ops").mints \
        == parse_profile(BOUNDED).get("ops").mints
    # and the same axis-completeness refusal is reachable from JSON
    for bad in ('{"operators": [{"token": "ops", "mints": [{"capability": "*",'
                ' "uses": 3}]}]}',
                '{"operators": [{"token": "ops", "mints": [{"capability": "*",'
                ' "ttl": "1m"}]}]}'):
        with pytest.raises(ProfileError):
            parse_profile(bad)


# ---------------------------------------------------------------------------
# The decision, as a pure comparison
# ---------------------------------------------------------------------------

def test_a_mint_inside_the_declaration_is_admitted():
    """NON-VACUITY. The declared cone admits a narrower one, and both numeric
    axes admit a smaller number."""
    ops = parse_profile(BOUNDED).get("ops")
    assert mint_refusal(ops, capability=_TMP, uses=10,
                        ttl_ms=30 * 60 * 1000) is None
    assert mint_refusal(ops, capability=_TMP_JOB, uses=1, ttl_ms=1000) is None


def test_a_mint_outside_the_declared_cone_is_refused():
    """The object dimension is `cap_order.covers`, read over a DECLARATION
    instead of over an authority. A sibling path is not inside the cone."""
    ops = parse_profile(BOUNDED).get("ops")
    finding = mint_refusal(ops, capability=_ETC, uses=1, ttl_ms=1000)
    assert finding is not None
    assert "fs.write(path=\"/etc\")" in finding
    assert "may mint" in finding


def test_a_mint_above_the_declared_uses_is_refused():
    ops = parse_profile(BOUNDED).get("ops")
    finding = mint_refusal(ops, capability=_TMP, uses=11, ttl_ms=1000)
    assert finding is not None
    assert "uses 11" in finding and "uses 10" in finding


def test_a_mint_above_the_declared_window_is_refused():
    ops = parse_profile(BOUNDED).get("ops")
    finding = mint_refusal(ops, capability=_TMP, uses=1,
                           ttl_ms=30 * 60 * 1000 + 1)
    assert finding is not None
    assert "ttl" in finding


def test_a_mint_that_bounds_neither_number_is_refused_against_a_bound():
    """FAILURE DIRECTION, on both numeric axes. A grant bounded only by its
    window bounds no number of uses, and one bounded only by its uses lives for
    the whole session. Neither can be SHOWN to be within a stated bound, so
    neither is admitted, and neither is silently clamped to the declared number
    (that would mint something the operator did not ask for)."""
    ops = parse_profile(BOUNDED).get("ops")
    assert mint_refusal(ops, capability=_TMP, uses=None,
                        ttl_ms=1000) is not None
    assert mint_refusal(ops, capability=_TMP, uses=1,
                        ttl_ms=None) is not None


def test_the_ceiling_dimension_is_the_declaration_too():
    """The third thing the issue names. A declaration stating `size="1MB"`
    admits a mint at or below it and refuses one above it; and a mint that
    states no amount at all against a stated ceiling is refused, because a
    ceiling the declaration bounded is not a free one."""
    ops = parse_profile("operator ops may mint "
                        'fs.write(path="/tmp", size="1MB") '
                        "uses 10 ttl 30m").get("ops")
    assert mint_refusal(ops, capability=_TMP_1MB, uses=1, ttl_ms=1) is None
    assert mint_refusal(ops, capability=_TMP_10MB, uses=1,
                        ttl_ms=1) is not None
    assert mint_refusal(ops, capability=_TMP, uses=1, ttl_ms=1) is not None


def test_several_declarations_are_alternatives():
    """A profile that means two cones writes two lines, because relatedness
    between capabilities is declared and never inferred. So the lines are
    alternatives: requiring a mint to satisfy every line at once would make the
    second refuse everything the first admits."""
    ops = parse_profile(
        'operator ops may mint fs.write(path="/tmp") uses 10 ttl 30m\n'
        "operator ops may mint model.complete uses 2 ttl 1m\n").get("ops")
    assert mint_refusal(ops, capability=_TMP, uses=10, ttl_ms=1000) is None
    assert mint_refusal(ops, capability="model.complete", uses=2,
                        ttl_ms=1000) is None
    # the numbers do not cross between lines
    assert mint_refusal(ops, capability="model.complete", uses=10,
                        ttl_ms=1000) is not None
    finding = mint_refusal(ops, capability=_ETC, uses=1, ttl_ms=1000)
    assert finding is not None
    # a mint outside every line names every line it was compared against
    assert 'may mint fs.write(path="/tmp")' in finding
    assert "may mint model.complete" in finding


def test_an_operator_with_no_declaration_is_unchanged():
    """MIGRATION, as a pure comparison. A profile written before this grammar
    existed declares nothing and is bounded by nothing new."""
    legacy = parse_profile(LEGACY).get("ops")
    assert not legacy.bounds_mints()
    assert mint_refusal(legacy, capability=_ETC, uses=10 ** 6,
                        ttl_ms=10 ** 9) is None


def test_no_profile_at_all_is_unchanged():
    """Item 55 is opt-in: with no profile bound there is no operator to have
    declared anything, and the mint is ungated exactly as every verb is."""
    assert mint_refusal(None, capability=_ETC, uses=10 ** 6,
                        ttl_ms=10 ** 9) is None


def test_a_mint_declaration_is_not_a_verb_grant():
    """The two records answer different questions and compose rather than
    overlap. Declaring what may be minted does not grant the authority to
    approve anything."""
    ops = parse_profile('operator ops may mint fs.write(path="/tmp") '
                        "uses 10 ttl 30m").get("ops")
    assert ops.bounds_mints()
    assert not ops.allows("approve", frozenset({"Agent"}))[0]


# ---------------------------------------------------------------------------
# The session: every route to a standing grant
# ---------------------------------------------------------------------------

def test_a_mint_inside_the_declaration_still_lands():
    """NON-VACUITY at the session. Passes on the branch before this one too —
    the refusals below are the comparison refusing, not the gate refusing
    everything."""
    session = _session()
    out = session.mint_standing_grant(capability=_TMP_JOB, uses=3,
                                      ttl_ms=60_000)
    assert out["granted"] is True
    assert session._grants[-1]["capability"] == _TMP_JOB


def test_the_session_refuses_a_mint_outside_the_declared_cone():
    """FAILS ON MAIN, where `approve` alone minted over any capability."""
    session = _session()
    with pytest.raises(SessionError) as exc:
        session.mint_standing_grant(capability=_ETC, uses=3, ttl_ms=60_000)
    assert "may not mint" in str(exc.value)
    assert session._grants == []


def test_the_session_refuses_a_mint_above_the_declared_uses():
    """FAILS ON MAIN, where `uses` was bounded by nothing but its own
    positivity check."""
    session = _session()
    with pytest.raises(SessionError) as exc:
        session.mint_standing_grant(capability=_TMP, uses=1000, ttl_ms=60_000)
    assert "uses 10" in str(exc.value)
    assert session._grants == []


def test_the_session_refuses_a_mint_above_the_declared_window():
    """FAILS ON MAIN. A standing grant had to be bounded, and nothing said by
    how much."""
    session = _session()
    with pytest.raises(SessionError):
        session.mint_standing_grant(capability=_TMP, uses=1,
                                    ttl_ms=24 * 3600 * 1000)
    assert session._grants == []


def test_the_session_refuses_a_grant_bounded_only_by_its_window():
    """The fail-closed direction at the session: `uses` unstated is unbounded
    uses, and the declaration permits at most ten."""
    session = _session()
    with pytest.raises(SessionError):
        session.mint_standing_grant(capability=_TMP, ttl_ms=60_000)
    assert session._grants == []


def test_the_calls_spelling_is_bounded_through_the_uses_axis():
    """A mint names its uses in two ways — `uses=N` and a `calls=N` on the
    spelling, which `_mint_grant` folds into the same counter. The declaration
    bounds the folded number, so one quantity stays bounded by one rule."""
    session = _session()
    with pytest.raises(SessionError) as exc:
        session.mint_standing_grant(
            capability='fs.write(path="/tmp", calls=500)', ttl_ms=1000)
    assert "uses 10" in str(exc.value)
    assert session._grants == []
    # and the same spelling inside the bound lands, folded into the counter
    session.mint_standing_grant(
        capability='fs.write(path="/tmp", calls=4)', ttl_ms=1000)
    assert session._grants[-1]["remainingUses"] == 4


def test_the_ticket_route_is_bounded_too():
    """A mint from an outstanding ticket names no capability in its arguments,
    which is why the bound is enforced at `_mint_grant` (where the ticket's own
    class-(c) spelling has been resolved) rather than at the verb gate."""
    session = _session()
    session._tickets["h1"] = {
        "hash": "h1", "component": "Agent", "candidateHash": "h0",
        "capabilities": [_ETC], "classCCapabilities": [_ETC]}
    with pytest.raises(SessionError) as exc:
        session.mint_standing_grant(ticket_hash="h1", uses=1, ttl_ms=1000)
    assert "may not mint" in str(exc.value)
    assert session._grants == []


def test_a_profile_with_no_mint_line_mints_exactly_as_before():
    """MIGRATION at the session, and the half that keeps this adoptable: an
    existing profile that legitimately mints broad grants goes on doing so."""
    session = _session(profile=LEGACY)
    out = session.mint_standing_grant(capability=_ETC, uses=10 ** 6,
                                      ttl_ms=10 ** 9)
    assert out["granted"] is True


def test_a_session_with_no_operator_mints_exactly_as_before():
    session = _session(profile=None)
    assert session.mint_standing_grant(capability=_ETC, uses=10 ** 6,
                                       ttl_ms=10 ** 9)["granted"] is True


# ---------------------------------------------------------------------------
# The other way an operator installs a standing yes
# ---------------------------------------------------------------------------

def _offer(caps, uses=None, ttl_ms=None, component="Agent"):
    """The distillation offer `apply_distillation` folds, reduced to the fields
    it reads. The rule is the real `policy.AutoApproveRule`."""
    from revl.policy import AutoApproveRule
    rule = AutoApproveRule(component=component, caps=tuple(caps),
                           uses=uses, ttl_ms=ttl_ms)
    blast = types.SimpleNamespace(not_covered=(), covered=1, total=1)
    return types.SimpleNamespace(rule=rule, blast=blast, operator="ops",
                                 sessions=("s1",), grant_count=3)


def _with_offer(session, offer):
    session._offer_by_id = lambda offer_id: offer
    return session


def test_a_distilled_rule_answers_to_the_same_declaration():
    """A distilled rule installs a STANDING auto-approve, which is the same
    authority as minting a standing grant. Bounding only the mint would leave
    this as the way around it."""
    session = _with_offer(_session(), _offer([_ETC], uses=1, ttl_ms=1000))
    with pytest.raises(SessionError) as exc:
        session.apply_distillation("offer-1")
    assert "may not mint" in str(exc.value)
    assert not getattr(session.sandbox, "auto_approve_rules", ())


def test_a_distilled_rule_inside_the_declaration_still_applies():
    """NON-VACUITY for the same route."""
    session = _with_offer(_session(), _offer([_TMP], uses=5, ttl_ms=1000))
    assert session.apply_distillation("offer-1")["applied"] is True
    assert len(session.sandbox.auto_approve_rules) == 1


def test_a_distilled_rule_bounding_nothing_is_refused_against_a_bound():
    """A rule with neither `uses` nor `ttl` is an unbounded standing yes, and an
    unbounded one cannot be within a stated bound."""
    session = _with_offer(_session(), _offer([_TMP]))
    with pytest.raises(SessionError):
        session.apply_distillation("offer-1")


def test_distillation_is_unchanged_for_a_profile_that_declares_nothing():
    """MIGRATION, on this route too."""
    session = _with_offer(_session(profile=LEGACY), _offer([_ETC]))
    assert session.apply_distillation("offer-1")["applied"] is True


def test_the_operator_module_exposes_one_decision():
    """One enforcement point. `mint_refusal` is the whole comparison, and
    `session._mint_grant` is the one implementation every route to a standing
    grant runs through, so a fourth route cannot quietly forget it."""
    assert callable(op.mint_refusal)
    assert op.MINT_VERB == "mint"
    assert op.ANY_CAPABILITY == "*" and op.UNBOUNDED == "*"
    assert isinstance(Operator("x").mints, tuple)

"""Item 441 controller slice 1: the termination lattice
(docs/design/441-goal-contracts.md §3.2, and T6 from §11.2).

These assertions ARE the design's claims about the fold, so the recommendation
can be re-checked by running the file rather than rereading the argument.
"""

import itertools

import pytest

from revl.goal import GoalState, classify, fold, worse

ALL_STATES = list(GoalState)


def test_exactly_four_states_no_more():
    # T6: the state is one of exactly four symbols.
    assert {s.value for s in GoalState} == {
        "satisfied",
        "undecided",
        "unevaluable",
        "violated",
    }
    assert len(ALL_STATES) == 4


def test_declared_order_is_the_design_lattice():
    # §3.2: violated > unevaluable > undecided > satisfied.
    ranked = sorted(ALL_STATES, key=lambda s: [
        GoalState.SATISFIED,
        GoalState.UNDECIDED,
        GoalState.UNEVALUABLE,
        GoalState.VIOLATED,
    ].index(s))
    for lower, higher in itertools.pairwise(ranked):
        assert worse(lower, higher) is higher
        assert worse(higher, lower) is higher


def test_worse_is_commutative_idempotent_and_total():
    for a, b in itertools.product(ALL_STATES, repeat=2):
        assert worse(a, b) is worse(b, a)
    for a in ALL_STATES:
        assert worse(a, a) is a


def test_satisfied_is_the_least_element():
    # A run is done only when NOTHING else has anything to say: satisfied loses
    # to every other state.
    for other in ALL_STATES:
        assert worse(GoalState.SATISFIED, other) is other


def test_violated_dominates_everything():
    for other in ALL_STATES:
        assert worse(GoalState.VIOLATED, other) is GoalState.VIOLATED


def test_fold_is_a_max_over_the_order():
    assert fold([GoalState.SATISFIED, GoalState.SATISFIED]) is GoalState.SATISFIED
    assert fold(
        [GoalState.SATISFIED, GoalState.UNDECIDED]
    ) is GoalState.UNDECIDED
    assert fold(
        [GoalState.UNDECIDED, GoalState.UNEVALUABLE, GoalState.SATISFIED]
    ) is GoalState.UNEVALUABLE
    assert fold(
        [GoalState.SATISFIED, GoalState.VIOLATED, GoalState.UNDECIDED]
    ) is GoalState.VIOLATED


def test_empty_fold_is_refused_not_vacuously_satisfied():
    # The null-contract attack: an all()-over-booleans fold would return True
    # here. The design refuses it instead.
    with pytest.raises(ValueError):
        fold([])


def test_all_satisfied_is_the_only_route_to_satisfied():
    # `satisfied` is reachable from a fold ONLY when every finding is satisfied.
    for combo in itertools.product(ALL_STATES, repeat=3):
        result = fold(combo)
        if result is GoalState.SATISFIED:
            assert all(s is GoalState.SATISFIED for s in combo)


def test_classify_never_reads_a_reply():
    # The static half of the "replace the agent with a liar and nothing
    # changes" exit criterion (§11.1 run 2): classify has no reply channel, so
    # identical observations produce identical states regardless of any claim.
    assert classify("criterion", True, served=True) is GoalState.SATISFIED
    assert classify("criterion", False, served=True) is GoalState.UNDECIDED
    assert classify("criterion", None, served=True) is GoalState.UNDECIDED


def test_classify_guard_role():
    assert classify("guard", True, served=True) is GoalState.VIOLATED
    assert classify("guard", False, served=True) is GoalState.SATISFIED
    assert classify("guard", None, served=True) is GoalState.UNDECIDED


def test_not_served_is_unevaluable_never_undecided():
    # §7 E2 / item 427 F1: a drifted verifier fails closed to unevaluable, not
    # undecided. True regardless of role or the stale value.
    for role in ("criterion", "guard"):
        for value in (True, False, None):
            assert classify(role, value, served=False) is GoalState.UNEVALUABLE


def test_classify_rejects_unknown_role():
    with pytest.raises(ValueError):
        classify("emission", True, served=True)


def test_no_finding_carries_a_float():
    # T6: no scoring. States are symbols; nothing here is orderable by a scalar
    # a caller could threshold on.
    for role in ("criterion", "guard"):
        for value in (True, False, None):
            for served in (True, False):
                assert isinstance(
                    classify(role, value, served=served), GoalState
                )

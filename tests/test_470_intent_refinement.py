"""Roadmap item 470 / issue #822, slice 1: the intent-refinement kernel
(docs/design/470-intent-refinement.md).

These assertions ARE the design's claims about `refine`, so the note can be
re-checked by running the file rather than by rereading the argument. The two
properties that matter are:

  * the object dimension is exactly `cap_order.covers` read with the intent as
    the wider side, so nothing `covers` refuses is admitted here; and
  * no dimension is free by OMISSION, so each of the three fail-closed
    asymmetries (unstated tenant, unstated amount, spend on an unstated ceiling)
    is refused rather than defaulted to permission.
"""

import itertools

import pytest

from revl.cap_order import covers, make_cap
from revl.intent import (
    Action,
    Intent,
    Refusal,
    Violation,
    ceiling_params,
    refine,
)


def _intent(spelling, verbs=("op",), tenant=None):
    """An intent built from the same capability spelling the policy, the G4
    check and the approval gate already speak."""
    return Intent.from_cap(make_cap(*spelling), verbs=verbs, tenant=tenant)


def _action(spelling, verb="op", amounts=(), tenant=None):
    return Action(make_cap(*spelling), verb, tuple(amounts), tenant)


# --------------------------------------------------------------- admitted


def test_a_refinement_of_every_dimension_is_admitted():
    intent = _intent(
        ("fs.write", [("path", "/tmp"), ("calls", 3)]),
        verbs=("write", "append"),
        tenant="billing",
    )
    action = _action(
        ("fs.write", [("path", "/tmp/job-42")]),
        verb="write",
        amounts=(("calls", 2),),
        tenant="billing",
    )
    assert refine(intent, action) is None


def test_a_bare_token_intent_admits_every_scope_under_it():
    # A bare token is `covers`' own top of its cone, reused and not widened.
    intent = _intent(("fs.write",), verbs=("write",))
    assert (
        refine(intent, _action(("fs.write", [("path", "/anywhere")]), verb="write"))
        is None
    )


def test_the_ceiling_bound_itself_is_within_the_ceiling():
    intent = _intent(("model.complete", [("calls", 3)]))
    assert refine(intent, _action(("model.complete",), amounts=(("calls", 3),))) is None


# --------------------------------------------------------------- the object


def test_a_different_token_is_an_extra_capability():
    refusal = refine(
        _intent(("fs.write", [("path", "/tmp")])),
        _action(("fs.read", [("path", "/tmp")])),
    )
    assert refusal is not None
    assert refusal.violation is Violation.EXTRA_CAPABILITY
    # The confused-deputy case: the action reaches a boundary the intent never
    # named, and distinct tokens are incomparable in the order.
    assert refusal.declared == 'fs.write(path="/tmp")'
    assert refusal.requested == 'fs.read(path="/tmp")'


def test_a_sibling_path_is_an_object_violation_not_an_extra_capability():
    refusal = refine(
        _intent(("fs.write", [("path", "/tmp")])),
        _action(("fs.write", [("path", "/etc")])),
    )
    assert refusal is not None
    assert refusal.violation is Violation.OBJECT
    assert refusal.dimension == "object"


def test_a_discrete_mismatch_is_an_object_violation():
    refusal = refine(
        _intent(("net.fetch", [("host", "prod")])),
        _action(("net.fetch", [("host", "dev")])),
    )
    assert refusal is not None
    assert refusal.violation is Violation.OBJECT


def test_a_dropped_parameter_is_a_wider_request_not_a_relaxing_one():
    # `covers` refuses `b` that drops a parameter `a` binds: the bare action is
    # WIDER than the declared cone.
    refusal = refine(_intent(("fs.write", [("path", "/tmp")])), _action(("fs.write",)))
    assert refusal is not None
    assert refusal.violation is Violation.OBJECT


def test_refine_is_exactly_covers_when_only_the_object_dimension_is_in_play():
    # The load-bearing non-regression: the kernel is a READING of the tree's one
    # partial order, so it admits precisely the pairs `covers` admits. Every
    # action `covers` refuses is refused here too.
    spellings = [
        ("fs.write", []),
        ("fs.write", [("path", "/tmp")]),
        ("fs.write", [("path", "/tmp/job-42")]),
        ("fs.write", [("path", "/etc")]),
        ("fs.read", [("path", "/tmp")]),
        ("net.fetch", [("host", "prod")]),
        ("net.fetch", [("host", "dev")]),
        ("*", []),
    ]
    for wide, narrow in itertools.product(spellings, repeat=2):
        intent = _intent(wide)
        action = _action(narrow)
        admitted = refine(intent, action) is None
        assert admitted is covers(make_cap(*wide), make_cap(*narrow)), (
            f"covers and refine disagree on {wide} vs {narrow}"
        )


# --------------------------------------------------------------- the verb


def test_a_disallowed_verb_is_refused():
    refusal = refine(
        _intent(("fs.write", [("path", "/tmp")]), verbs=("write",)),
        _action(("fs.write", [("path", "/tmp")]), verb="unlink"),
    )
    assert refusal is not None
    assert refusal.violation is Violation.VERB
    assert refusal.dimension == "verbs"
    assert refusal.requested == "unlink"
    assert refusal.declared == "write"


def test_an_empty_verb_set_refuses_every_action():
    # The empty set is a legitimate contract ("no operation is permitted"), and
    # it is not a spelling of "unconstrained": fail closed.
    intent = _intent(("fs.write", [("path", "/tmp")]), verbs=())
    for verb in ("write", "read", "unlink", ""):
        refusal = refine(intent, _action(("fs.write", [("path", "/tmp")]), verb=verb))
        assert refusal is not None
        assert refusal.violation is Violation.VERB
    assert _intent(("fs.write",), verbs=()).verbs == frozenset()


def test_the_verb_bypasses_nothing_else():
    # An action well inside the object cone and the ceiling, but off-verb, is
    # still refused: the verb is part of the declared intent.
    intent = _intent(("model.complete", [("calls", 3)]), verbs=("complete",))
    action = _action(("model.complete",), verb="cancel", amounts=(("calls", 1),))
    refusal = refine(intent, action)
    assert refusal is not None
    assert refusal.violation is Violation.VERB


def test_a_bare_string_verb_declaration_is_refused_not_split_into_characters():
    # `frozenset("write")` is the five single-character verbs, so the intended
    # verb would be refused while `w` was admitted: a widening, not a narrowing.
    # The declaration is refused instead of guessed at (fail closed).
    with pytest.raises(ValueError):
        _intent(("m",), verbs="write")

    intent = _intent(("m",), verbs={"write"})
    assert intent.verbs == frozenset({"write"})
    # The intended verb is admitted, and the character the bare string used to
    # declare is not: a multi-character verb cannot be reached one letter wide.
    assert refine(intent, _action(("m",), verb="write")) is None
    refusal = refine(intent, _action(("m",), verb="w"))
    assert refusal is not None
    assert refusal.violation is Violation.VERB
    assert refusal.declared == "write"


def test_a_verb_that_is_not_a_verb_name_is_refused():
    with pytest.raises(ValueError):
        _intent(("m",), verbs=("op", 3))
    with pytest.raises(ValueError):
        _intent(("m",), verbs=b"op")


# --------------------------------------------------------------- the ceiling


def test_an_amount_above_the_stated_ceiling_is_refused():
    refusal = refine(
        _intent(("model.complete", [("calls", 3)])),
        _action(("model.complete",), amounts=(("calls", 4),)),
    )
    assert refusal is not None
    assert refusal.violation is Violation.CEILING
    assert refusal.declared == "calls=3"
    assert refusal.requested == "calls=4"


def test_an_unstated_amount_against_a_stated_ceiling_is_refused():
    # Fail closed: an unstated amount is not an amount within the ceiling.
    refusal = refine(
        _intent(("model.complete", [("calls", 3)])), _action(("model.complete",))
    )
    assert refusal is not None
    assert refusal.violation is Violation.CEILING
    assert refusal.requested == "calls=<unstated>"


def test_a_spend_on_an_unstated_ceiling_is_refused():
    # The intent bounded a different quantity, so it has not authorized this
    # spend. This is the asymmetry the roadmap's "higher amount" case hides.
    refusal = refine(
        _intent(("fs.write", [("path", "/tmp")])),
        _action(("fs.write", [("path", "/tmp")]), amounts=(("size", 10),)),
    )
    assert refusal is not None
    assert refusal.violation is Violation.CEILING
    assert refusal.declared == "none"
    assert refusal.requested == "size=10"


def test_no_ceiling_stated_and_none_spent_is_admitted():
    # The exhaustive-intent case: the intent says nothing about a ceiling and the
    # action spends nothing, so there is nothing to refuse.
    intent = _intent(("fs.write", [("path", "/tmp")]))
    assert intent.ceilings == ()
    assert refine(intent, _action(("fs.write", [("path", "/tmp")]))) is None


def test_a_zero_spend_against_a_stated_ceiling_is_admitted():
    # The bound is the intent, and spending none of it is inside it.
    intent = _intent(("model.complete", [("calls", 3)]))
    assert refine(intent, _action(("model.complete",), amounts=(("calls", 0),))) is None


def test_a_negative_declared_spend_is_refused():
    # A negative spend satisfies `spent[name] > bound` for every non-negative
    # bound, so it reads as proof of compliance: the one true sign fail-open in
    # this dimension. It is refused where `cap_order._canon_value` refuses a
    # negative parsed ceiling, at construction of the record that states it.
    with pytest.raises(ValueError):
        _action(("m",), amounts=(("calls", -5),))
    with pytest.raises(ValueError):
        Action(make_cap("m"), "op", (("calls", -1),), None)
    with pytest.raises(ValueError):
        Intent(make_cap("m"), frozenset({"op"}), (("calls", -1),), None)


def test_a_non_numeric_amount_or_bound_is_refused_never_a_bare_type_error():
    # A `str` amount made the comparison raise a bare TypeError out of `refine`,
    # which is neither the record's contract (an unbuildable declaration) nor
    # `refine`'s (fail closed through `Refusal`). `_canonical_ceilings` is the
    # single validation point for both directions, so it refuses the value.
    with pytest.raises(ValueError):
        _action(("m",), amounts=(("calls", "3"),))
    with pytest.raises(ValueError):
        Action(make_cap("m"), "op", (("calls", "3"),), None)
    with pytest.raises(ValueError):
        Intent(make_cap("m"), frozenset({"op"}), (("calls", "3"),), None)
    with pytest.raises(ValueError):
        ceiling_params({"calls": "3"})
    # `bool` IS an `int` in Python, so `calls=True` would compare as `calls=1`.
    with pytest.raises(ValueError):
        _action(("m",), amounts=(("calls", True),))


def test_refine_is_total_when_a_record_bypassed_the_construction_check():
    # Unpickling a frozen dataclass rebuilds it with `__new__` plus a state
    # assignment, so `__post_init__` does not run and a value the records refuse
    # can still be present. `refine` is fail-closed through `Refusal`, so an
    # uncomparable value is a refusal and never a bare TypeError out of the
    # comparison.
    intent = _intent(("m", [("calls", 3)]))
    valid = _action(("m",), amounts=(("calls", 1),))

    def _bypass(cls, **fields):
        record = object.__new__(cls)
        for name, value in fields.items():
            object.__setattr__(record, name, value)
        return record

    for planted in ((("calls", -5),), (("calls", "1"),), (("calls", True),)):
        bypassed = _bypass(
            Action,
            object=valid.object,
            verb=valid.verb,
            amounts=planted,
            tenant=valid.tenant,
        )
        refusal = refine(intent, bypassed)
        assert isinstance(refusal, Refusal)
        assert refusal.violation is Violation.CEILING

    bypassed_intent = _bypass(
        Intent,
        object=intent.object,
        verbs=intent.verbs,
        ceilings=(("calls", "3"),),
        tenant=None,
    )
    refusal = refine(bypassed_intent, valid)
    assert isinstance(refusal, Refusal)
    assert refusal.violation is Violation.CEILING


def test_each_ceiling_dimension_is_compared_independently():
    intent = _intent(("m", [("calls", 3), ("size", 100)]))
    assert intent.ceiling_map() == {"calls": 3, "size": 100}
    admitted = _action(("m",), amounts=(("calls", 3), ("size", 100)))
    assert refine(intent, admitted) is None
    over = _action(("m",), amounts=(("calls", 3), ("size", 101)))
    assert refine(intent, over).violation is Violation.CEILING


# --------------------------------------------------------------- the tenant


def test_a_different_tenant_is_refused():
    refusal = refine(
        _intent(("db.query",), tenant="billing"),
        _action(("db.query",), tenant="ledger"),
    )
    assert refusal is not None
    assert refusal.violation is Violation.TENANT
    assert refusal.declared == "billing"
    assert refusal.requested == "ledger"


def test_an_action_with_no_tenant_cannot_be_in_a_stated_tenant():
    refusal = refine(_intent(("db.query",), tenant="billing"), _action(("db.query",)))
    assert refusal is not None
    assert refusal.violation is Violation.TENANT
    assert refusal.requested == "<unstated>"


def test_an_unstated_intent_tenant_does_not_constrain_tenancy():
    # `None` is a STATED absence: the intent does not constrain tenancy, so the
    # action's tenant (whether named or not) is not a violation.
    intent = _intent(("db.query",))
    assert intent.tenant is None
    assert refine(intent, _action(("db.query",), tenant="anything")) is None
    assert refine(intent, _action(("db.query",))) is None


# --------------------------------------------------------------- the typed link


def test_from_cap_splits_ceilings_out_of_the_declared_spelling():
    intent = _intent(("fs.write", [("path", "/tmp"), ("calls", 3)]))
    assert intent.object.to_str() == 'fs.write(path="/tmp")'
    assert intent.object.params == (("path", ("tmp",)),)
    assert intent.ceilings == (("calls", 3),)


def test_from_cap_resolves_the_budget_aliases():
    # `requests`/`bytes` canonicalize at parse, so one spelling and one stored
    # parameter; the ceiling split then reads the canonical name.
    intent = _intent(("m", [("requests", 2), ("bytes", 64)]))
    assert intent.ceilings == (("calls", 2), ("size", 64))


def test_from_cap_keeps_a_bare_token_bare():
    intent = _intent(("m",))
    assert intent.object.to_str() == "m"
    assert intent.ceilings == ()


def test_ceiling_params_is_sorted_and_total():
    assert ceiling_params({"size": 2, "calls": 1}) == (("calls", 1), ("size", 2))
    assert ceiling_params({}) == ()


def test_ceiling_params_refuses_a_resource_kind_name():
    # One fact, one dimension: a resource parameter in the ceiling map would state
    # a bound the object dimension also states.
    for name in ("path", "host", "table"):
        with pytest.raises(ValueError):
            ceiling_params({name: 1})


def test_a_hand_built_intent_is_canonicalized_like_one_built_from_cap():
    # `_canonical_ceilings` claims to be the single validation point for both
    # directions; `Intent.__post_init__` did not route through it, so a
    # hand-built record could hold ceilings `from_cap` would have refused.
    intent = Intent(make_cap("m"), ["op", "write"], (("size", 2), ("calls", 1)), None)
    assert intent.verbs == frozenset({"op", "write"})
    assert intent.ceilings == (("calls", 1), ("size", 2))


def test_a_hand_built_intent_goes_through_the_check_from_cap_routes_through():
    with pytest.raises(ValueError):
        Intent(make_cap("m"), frozenset({"op"}), (("path", 1),), None)
    with pytest.raises(ValueError):
        Intent(make_cap("m"), frozenset({"op"}), (("calls", 1), ("calls", 2)), None)


def test_the_records_refuse_a_ceiling_parameter_on_the_object():
    cap = make_cap("fs.write", [("path", "/tmp"), ("calls", 3)])
    with pytest.raises(ValueError):
        Intent(cap, frozenset({"write"}), (), None)
    with pytest.raises(ValueError):
        Action(cap, "write", (), None)


def test_action_amounts_go_through_the_same_check_as_an_intents_ceilings():
    # One fact, one dimension: a resource parameter states a bound the object
    # dimension also states, and a name bound twice would let the last spelling
    # silently win.
    assert _action(("m",), amounts=(("size", 2), ("calls", 1))).amounts == (
        ("calls", 1),
        ("size", 2),
    )
    with pytest.raises(ValueError):
        _action(("m",), amounts=(("path", 1),))
    with pytest.raises(ValueError):
        _action(("m",), amounts=(("calls", 1), ("calls", 2)))


def test_every_field_is_required_so_a_half_written_intent_cannot_read_as_free():
    # No defaults: a record that omits a dimension is a TypeError, not an
    # unconstrained record.
    with pytest.raises(TypeError):
        Intent(make_cap("fs.write", [("path", "/tmp")]), frozenset({"write"}))
    with pytest.raises(TypeError):
        Action(make_cap("fs.write", [("path", "/tmp")]), "write")
    with pytest.raises(TypeError):
        Intent.from_cap(make_cap("fs.write"), verbs=("write",))


# --------------------------------------------------------------- the refusal


def test_the_five_violations_are_the_ones_the_exit_criterion_names():
    assert {v.value for v in Violation} == {
        "extra_capability",
        "object",
        "verb",
        "ceiling",
        "tenant",
    }


def test_the_dimensions_are_checked_in_the_order_the_intent_declares_them():
    # An action that exceeds several dimensions reports the FIRST stated one, so
    # a caller that fixes one and re-runs gets the next.
    intent = _intent(
        ("fs.write", [("path", "/tmp"), ("calls", 3)]),
        verbs=("write",),
        tenant="billing",
    )
    everything_wrong = _action(
        ("fs.read", [("path", "/etc")]),
        verb="unlink",
        amounts=(("calls", 9),),
        tenant="ledger",
    )
    assert refine(intent, everything_wrong).violation is Violation.EXTRA_CAPABILITY

    wrong_verb_only = _action(
        ("fs.write", [("path", "/tmp/job-1")]),
        verb="unlink",
        amounts=(("calls", 1),),
        tenant="billing",
    )
    assert refine(intent, wrong_verb_only).violation is Violation.VERB

    wrong_ceiling_only = _action(
        ("fs.write", [("path", "/tmp/job-1")]),
        verb="write",
        amounts=(("calls", 9),),
        tenant="billing",
    )
    assert refine(intent, wrong_ceiling_only).violation is Violation.CEILING


def test_a_refusal_renders_the_message_and_hint_the_errors_module_uses():
    refusal = refine(
        _intent(("fs.write", [("path", "/tmp")]), verbs=("write",)),
        _action(("fs.write", [("path", "/tmp")]), verb="unlink"),
    )
    assert isinstance(refusal, Refusal)
    rendered = str(refusal)
    assert rendered == f"{refusal.message}\n  {refusal.hint}"
    assert rendered.count("\n") == 1
    # The roadmap's exit criterion: refused WITH the intent it violated.
    assert refusal.declared in rendered


def test_every_refusal_names_the_declared_and_requested_values():
    intent = _intent(("fs.write", [("path", "/tmp")]))
    action = _action(("fs.read", [("path", "/tmp")]))
    refusal = refine(intent, action)
    assert refusal.declared == intent.object.to_str()
    assert refusal.requested == action.object.to_str()
    assert refusal.message and refusal.hint

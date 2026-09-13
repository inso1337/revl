"""Roadmap item 470 / issue #822, slices 1-2: the intent-refinement kernel
(docs/design/470-intent-refinement.md).

These assertions ARE the design's claims about `refine`, so the note can be
re-checked by running the file rather than by rereading the argument. The two
properties that matter are:

  * the object dimension is exactly `cap_order.covers` read with each capability
    the intent names as the wider side, so nothing `covers` refuses under any
    declared object is admitted here; and
  * no dimension is free by OMISSION, so each fail-closed asymmetry (unstated
    tenant, unstated amount, spend on an unstated ceiling, unstated scope,
    members in an unstated scope) is refused rather than defaulted to permission.

Slice 2 adds the two clauses the issue names and slice 1 did not model: the
EXPLICITLY-related object, and the set-valued scope that carries "no broader
recipient scope" and "no broader data scope".
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
    scope_members,
)


def _intent(spelling, verbs=("op",), tenant=None, related=(), scopes=()):
    """An intent built from the same capability spelling the policy, the G4
    check and the approval gate already speak."""
    return Intent.from_cap(
        make_cap(*spelling),
        verbs=verbs,
        tenant=tenant,
        related=tuple(make_cap(*one) for one in related),
        scopes=scopes,
    )


def _action(spelling, verb="op", amounts=(), tenant=None, scopes=()):
    return Action(make_cap(*spelling), verb, tuple(amounts), tenant, tuple(scopes))


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


def test_the_violations_are_the_ones_the_exit_criterion_and_the_issue_name():
    # The roadmap's exit criterion names four (a wider verb, a higher amount, a
    # different tenant, an extra capability); `object` is the fifth because
    # `covers` refuses the same token outside its cone differently from a token
    # it never named, and `scope` is the issue's "no broader recipient scope".
    assert {v.value for v in Violation} == {
        "extra_capability",
        "object",
        "verb",
        "ceiling",
        "scope",
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


def test_the_scope_is_checked_after_the_ceiling_and_before_the_tenant():
    intent = _intent(
        ("mail.send", [("calls", 3)]),
        verbs=("send",),
        tenant="billing",
        scopes=(("recipients", {"alice"}),),
    )
    over_ceiling_too = _action(
        ("mail.send",),
        verb="send",
        amounts=(("calls", 9),),
        tenant="ledger",
        scopes=(("recipients", {"alice", "bob"}),),
    )
    assert refine(intent, over_ceiling_too).violation is Violation.CEILING

    scope_and_tenant = _action(
        ("mail.send",),
        verb="send",
        amounts=(("calls", 1),),
        tenant="ledger",
        scopes=(("recipients", {"alice", "bob"}),),
    )
    assert refine(intent, scope_and_tenant).violation is Violation.SCOPE

    tenant_only = _action(
        ("mail.send",),
        verb="send",
        amounts=(("calls", 1),),
        tenant="ledger",
        scopes=(("recipients", {"alice"}),),
    )
    assert refine(intent, tenant_only).violation is Violation.TENANT


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


# ------------------------------------------- the explicitly-related object


def test_an_action_on_an_explicitly_related_object_is_admitted():
    # "Same or EXPLICITLY-related object": the intent named `fs.read` beside the
    # object it is primarily about, so reaching it is a refinement.
    intent = _intent(
        ("fs.write", [("path", "/tmp")]),
        verbs=("write", "read"),
        related=(("fs.read", [("path", "/tmp")]),),
    )
    assert refine(intent, _action(("fs.read", [("path", "/tmp/in")]), verb="read")) is None
    assert (
        refine(intent, _action(("fs.write", [("path", "/tmp/out")]), verb="write"))
        is None
    )


def test_a_related_object_carries_its_own_cone_and_not_the_others():
    # Each declared object is read against `covers` on its own. `/etc` is under
    # neither cone, so naming a second object is not a widening of the first.
    intent = _intent(
        ("fs.write", [("path", "/tmp")]),
        verbs=("write", "read"),
        related=(("fs.read", [("path", "/var/log")]),),
    )
    assert refine(intent, _action(("fs.read", [("path", "/var/log/a")]), verb="read")) is None
    refusal = refine(intent, _action(("fs.read", [("path", "/tmp")]), verb="read"))
    assert refusal is not None
    assert refusal.violation is Violation.OBJECT
    assert refusal.requested == 'fs.read(path="/tmp")'


def test_relatedness_is_never_inferred_from_the_spelling():
    # Two boundaries that share a prefix, a scheme or a path are NOT related
    # unless the intent said so (item 470's scope note, one dimension down).
    intent = _intent(("fs.write", [("path", "/tmp")]), verbs=("write",))
    refusal = refine(intent, _action(("fs.read", [("path", "/tmp")]), verb="write"))
    assert refusal is not None
    assert refusal.violation is Violation.EXTRA_CAPABILITY


def test_a_token_no_declared_object_names_is_an_extra_capability():
    intent = _intent(
        ("fs.write", [("path", "/tmp")]),
        verbs=("write",),
        related=(("fs.read", [("path", "/tmp")]),),
    )
    refusal = refine(intent, _action(("net.fetch", [("host", "prod")]), verb="write"))
    assert refusal is not None
    assert refusal.violation is Violation.EXTRA_CAPABILITY
    # Refused WITH the intent it violated: every object the intent named.
    assert refusal.declared == 'fs.write(path="/tmp"), fs.read(path="/tmp")'
    assert refusal.requested == 'net.fetch(host="prod")'


def test_a_single_object_intent_renders_exactly_as_it_did_before_related():
    refusal = refine(
        _intent(("fs.write", [("path", "/tmp")])),
        _action(("fs.read", [("path", "/tmp")])),
    )
    assert refusal.declared == 'fs.write(path="/tmp")'


def test_the_other_dimensions_still_apply_to_a_related_object():
    # Matching through `related` admits the object dimension and nothing else.
    intent = _intent(
        ("fs.write", [("path", "/tmp")]),
        verbs=("write",),
        tenant="billing",
        related=(("fs.read", [("path", "/tmp")]),),
    )
    off_verb = _action(("fs.read", [("path", "/tmp")]), verb="read", tenant="billing")
    assert refine(intent, off_verb).violation is Violation.VERB
    off_tenant = _action(("fs.read", [("path", "/tmp")]), verb="write", tenant="ledger")
    assert refine(intent, off_tenant).violation is Violation.TENANT


def test_a_related_spelling_cannot_carry_a_ceiling():
    # One intent states one ceiling, on the primary spelling: a per-object
    # ceiling would be a second comparison the ceiling dimension never performs.
    with pytest.raises(ValueError):
        _intent(
            ("fs.write", [("path", "/tmp")]),
            related=(("fs.read", [("path", "/tmp"), ("calls", 9)]),),
        )
    with pytest.raises(ValueError):
        Intent(
            make_cap("fs.write"),
            frozenset({"write"}),
            (),
            None,
            (make_cap("fs.read", [("calls", 9)]),),
        )


def test_related_is_deduplicated_and_keeps_declaration_order():
    intent = _intent(
        ("fs.write", [("path", "/tmp")]),
        related=(
            ("net.fetch", [("host", "prod")]),
            ("fs.read", [("path", "/tmp")]),
            ("net.fetch", [("host", "prod")]),
        ),
    )
    assert [cap.to_str() for cap in intent.objects()] == [
        'fs.write(path="/tmp")',
        'net.fetch(host="prod")',
        'fs.read(path="/tmp")',
    ]


def test_a_related_entry_that_is_not_a_capability_is_refused():
    with pytest.raises(ValueError):
        Intent.from_cap(
            make_cap("fs.write"), verbs=("write",), tenant=None, related=("fs.read",)
        )
    with pytest.raises(ValueError):
        Intent(make_cap("fs.write"), frozenset({"write"}), (), None, ("fs.read",))


def test_related_defaults_to_naming_one_object():
    # The narrow reading: omitting `related` is not an unconstrained object set.
    intent = Intent(make_cap("fs.write"), frozenset({"write"}), (), None)
    assert intent.related == ()
    assert intent.objects() == (make_cap("fs.write"),)


# --------------------------------------------------- the scope (recipients, data)


def test_a_narrower_recipient_scope_is_admitted():
    intent = _intent(
        ("mail.send",), verbs=("send",), scopes=(("recipients", {"alice", "bob"}),)
    )
    action = _action(("mail.send",), verb="send", scopes=(("recipients", {"alice"}),))
    assert refine(intent, action) is None


def test_a_broader_recipient_scope_is_refused_and_names_the_extra_member():
    intent = _intent(
        ("mail.send",), verbs=("send",), scopes=(("recipients", {"alice"}),)
    )
    refusal = refine(
        intent,
        _action(("mail.send",), verb="send", scopes=(("recipients", {"alice", "bob"}),)),
    )
    assert refusal is not None
    assert refusal.violation is Violation.SCOPE
    assert refusal.dimension == "scopes"
    assert refusal.declared == "recipients={alice}"
    assert refusal.requested == "recipients={alice, bob}"
    # The member that escaped is named, so the fix is one name and not two sets.
    assert "bob" in refusal.message
    assert "recipients={bob}" in refusal.message


def test_a_broader_data_scope_is_refused():
    # The same containment rule over item 249's coarse data classes.
    intent = _intent(
        ("model.complete",), verbs=("complete",), scopes=(("data", {"input"}),)
    )
    refusal = refine(
        intent,
        _action(
            ("model.complete",), verb="complete", scopes=(("data", {"input", "secret"}),)
        ),
    )
    assert refusal is not None
    assert refusal.violation is Violation.SCOPE
    assert "secret" in refusal.message


def test_an_unstated_scope_against_a_stated_one_is_refused():
    intent = _intent(
        ("mail.send",), verbs=("send",), scopes=(("recipients", {"alice"}),)
    )
    refusal = refine(intent, _action(("mail.send",), verb="send"))
    assert refusal is not None
    assert refusal.violation is Violation.SCOPE
    assert refusal.requested == "recipients=<unstated>"


def test_members_in_a_scope_the_intent_does_not_state_are_refused():
    intent = _intent(("mail.send",), verbs=("send",))
    refusal = refine(
        intent, _action(("mail.send",), verb="send", scopes=(("recipients", {"bob"}),))
    )
    assert refusal is not None
    assert refusal.violation is Violation.SCOPE
    assert refusal.declared == "none"
    assert refusal.requested == "recipients={bob}"


def test_an_empty_requested_scope_is_always_admitted():
    # A set says what a count cannot: reaching nobody is a refinement of every
    # declaration, including one that never mentioned the scope.
    stated = _intent(
        ("mail.send",), verbs=("send",), scopes=(("recipients", {"alice"}),)
    )
    assert (
        refine(stated, _action(("mail.send",), verb="send", scopes=(("recipients", ()),)))
        is None
    )
    silent = _intent(("mail.send",), verbs=("send",))
    assert (
        refine(silent, _action(("mail.send",), verb="send", scopes=(("recipients", ()),)))
        is None
    )


def test_an_empty_declared_scope_refuses_every_member():
    # The empty declaration is a legitimate contract ("no party is permitted
    # here"), not a spelling of "unconstrained".
    intent = _intent(("mail.send",), verbs=("send",), scopes=(("recipients", ()),))
    refusal = refine(
        intent, _action(("mail.send",), verb="send", scopes=(("recipients", {"alice"}),))
    )
    assert refusal is not None
    assert refusal.violation is Violation.SCOPE
    assert refusal.declared == "recipients={}"


def test_each_scope_is_compared_independently():
    intent = _intent(
        ("mail.send",),
        verbs=("send",),
        scopes=(("recipients", {"alice"}), ("data", {"input", "fs"})),
    )
    good = _action(
        ("mail.send",),
        verb="send",
        scopes=(("recipients", {"alice"}), ("data", {"fs"})),
    )
    assert refine(intent, good) is None
    refusal = refine(
        intent,
        _action(
            ("mail.send",),
            verb="send",
            scopes=(("recipients", {"alice"}), ("data", {"fs", "secret"})),
        ),
    )
    assert refusal is not None
    assert refusal.declared == "data={fs, input}"


def test_a_bare_string_scope_member_set_is_refused_not_split_into_characters():
    # `frozenset("alice")` is five single-character members. On the ACTION side
    # that is a NARROWER request than the caller meant, so a check that iterated
    # the string could admit a send the intent never permitted.
    with pytest.raises(ValueError):
        _intent(("mail.send",), scopes=(("recipients", "alice"),))
    with pytest.raises(ValueError):
        _action(("mail.send",), scopes=(("recipients", "alice"),))


def test_a_scope_name_cannot_be_a_capability_parameter():
    # One fact, one dimension: `path`/`host` are the object dimension's and
    # `calls`/`size`/`time` (with the budget aliases) are the ceiling's.
    for name in ("path", "host", "table", "calls", "size", "time", "requests", "bytes"):
        with pytest.raises(ValueError):
            _intent(("fs.write",), scopes=((name, {"x"}),))
        with pytest.raises(ValueError):
            _action(("fs.write",), scopes=((name, {"x"}),))


def test_a_scope_bound_twice_is_refused():
    with pytest.raises(ValueError):
        _intent(
            ("mail.send",),
            scopes=(("recipients", {"alice"}), ("recipients", {"bob"})),
        )


def test_a_scope_member_that_is_not_a_name_is_refused():
    with pytest.raises(ValueError):
        _intent(("mail.send",), scopes=(("recipients", {"alice", 3}),))
    with pytest.raises(ValueError):
        _intent(("mail.send",), scopes=(("recipients", {""}),))
    with pytest.raises(ValueError):
        _intent(("mail.send",), scopes=((3, {"alice"}),))
    with pytest.raises(ValueError):
        _intent(("mail.send",), scopes=(("", {"alice"}),))


def test_scope_members_is_sorted_and_total():
    canonical = scope_members({"recipients": ("bob", "alice", "bob"), "data": ["fs"]})
    assert canonical == (
        ("data", frozenset({"fs"})),
        ("recipients", frozenset({"alice", "bob"})),
    )


def test_a_hand_built_record_canonicalizes_scopes_like_from_cap():
    # `_canonical_scopes` is the single validation point for both directions, and
    # it runs at construction rather than at comparison.
    intent = Intent(
        make_cap("mail.send"),
        frozenset({"send"}),
        (),
        None,
        (),
        (("recipients", ["bob", "alice"]),),
    )
    assert intent.scopes == (("recipients", frozenset({"alice", "bob"})),)
    action = Action(
        make_cap("mail.send"), "send", (), None, (("recipients", ["alice"]),)
    )
    assert action.scopes == (("recipients", frozenset({"alice"})),)
    assert action.scope_map() == {"recipients": frozenset({"alice"})}


def test_scopes_default_to_the_narrow_reading_on_both_records():
    assert Intent(make_cap("m"), frozenset({"op"}), (), None).scopes == ()
    assert Action(make_cap("m"), "op", (), None).scopes == ()
    # And the default on the intent side permits nothing rather than everything.
    refusal = refine(
        Intent(make_cap("m"), frozenset({"op"}), (), None),
        Action(make_cap("m"), "op", (), None, (("recipients", {"bob"}),)),
    )
    assert refusal is not None
    assert refusal.violation is Violation.SCOPE


# ------------------------------------------------- the typed link, action side


def test_action_from_cap_splits_the_spend_out_of_the_crossing_spelling():
    action = Action.from_cap(
        make_cap("model.complete", [("host", "prod"), ("calls", 2)]),
        verb="complete",
        tenant="billing",
    )
    assert action.object == make_cap("model.complete", [("host", "prod")])
    assert action.amounts == (("calls", 2),)
    assert action.tenant == "billing"


def test_action_from_cap_resolves_the_budget_aliases():
    action = Action.from_cap(
        make_cap("net.fetch", [("requests", 4), ("bytes", 16)]),
        verb="fetch",
        tenant=None,
    )
    assert action.amounts == (("calls", 4), ("size", 16))


def test_the_two_typed_links_read_one_spelling_the_same_way():
    # The same `Cap` spelling on both sides is the identity refinement: whatever
    # the declaration authorized, spending exactly it is within it.
    spelling = make_cap("model.complete", [("host", "prod"), ("calls", 2)])
    intent = Intent.from_cap(spelling, verbs=("complete",), tenant="billing")
    action = Action.from_cap(spelling, verb="complete", tenant="billing")
    assert refine(intent, action) is None


def test_action_from_cap_requires_the_verb_and_the_tenant():
    with pytest.raises(TypeError):
        Action.from_cap(make_cap("m"), verb="op")
    with pytest.raises(TypeError):
        Action.from_cap(make_cap("m"), tenant=None)


# ------------------------------------------- the whole contract, both directions


def test_an_honest_refinement_of_all_six_clauses_is_admitted():
    # The check is not vacuously strict: an action narrower on every clause the
    # intent states is admitted.
    intent = _intent(
        ("fs.write", [("path", "/tmp"), ("calls", 5)]),
        verbs=("write", "append"),
        tenant="billing",
        related=(("fs.read", [("path", "/tmp")]),),
        scopes=(("recipients", {"alice", "bob"}), ("data", {"fs", "input"})),
    )
    assert (
        refine(
            intent,
            _action(
                ("fs.write", [("path", "/tmp/job-42")]),
                verb="write",
                amounts=(("calls", 2),),
                tenant="billing",
                scopes=(("recipients", {"alice"}), ("data", {"fs"})),
            ),
        )
        is None
    )
    # And on the explicitly-related object, with the same narrowing.
    assert (
        refine(
            intent,
            _action(
                ("fs.read", [("path", "/tmp/in")]),
                verb="append",
                amounts=(("calls", 5),),
                tenant="billing",
                scopes=(("recipients", ()), ("data", {"fs", "input"})),
            ),
        )
        is None
    )


def test_each_clause_of_the_exit_criterion_refuses_and_names_the_clause():
    # The roadmap's exit criterion, one assertion per clause: refused WITH the
    # intent it violated, and the violated clause readable off the refusal.
    intent = _intent(
        ("fs.write", [("path", "/tmp"), ("calls", 5)]),
        verbs=("write",),
        tenant="billing",
        scopes=(("recipients", {"alice"}),),
    )
    base = {
        "verb": "write",
        "amounts": (("calls", 1),),
        "tenant": "billing",
        "scopes": (("recipients", {"alice"}),),
    }
    cases = {
        Violation.VERB: dict(base, verb="unlink"),
        Violation.CEILING: dict(base, amounts=(("calls", 9),)),
        Violation.TENANT: dict(base, tenant="ledger"),
        Violation.SCOPE: dict(base, scopes=(("recipients", {"alice", "bob"}),)),
    }
    for violation, overrides in cases.items():
        refusal = refine(intent, _action(("fs.write", [("path", "/tmp")]), **overrides))
        assert refusal is not None, violation
        assert refusal.violation is violation
        assert refusal.declared and refusal.requested
        assert refusal.declared in str(refusal)

    extra = refine(
        intent, _action(("shell.exec", [("host", "prod")]), **base)
    )
    assert extra.violation is Violation.EXTRA_CAPABILITY
    assert extra.declared in str(extra)

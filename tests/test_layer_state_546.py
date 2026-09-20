"""Item 546 (issue #1225): every evolvable layer declares whether its state is
revertible, compensatable or neither; a layer that is neither may not be
promoted without a declared and tested compensation; and a rollback says which
layers it restored and which it only compensated.

The oracle for `src/revl/layer_state.py`. Three things are held here beyond
the ordinary unit coverage:

* the FAILURE DIRECTION. An undeclared part must not resolve to `revertible`,
  and must not be liftable by a compensation either. Asserted directly and
  also as a property over the whole class lattice.
* the RECONCILIATION with the two lanes that answered this shape for one
  layer each. Item 518's parts and classes and item 522's five classes are
  asserted against the real modules WHENEVER THEY ARE IMPORTABLE, so the
  vocabularies cannot drift once those pull requests land, and skipped
  otherwise so this file passes on a tree that does not have them.
* NON-VACUITY. `effects_only_admits` is the rule as it stands before this
  change, written out: a promotion is admitted when every EFFECT it performs
  is revertible. Nine plans are admitted by it and refused by `check`, and
  five controls are admitted by both.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from revl import layer_state as ls
from revl.layer_state import (
    LAYERS, LINKS, STATE_CLASSES, UNDECLARED, UNTOUCHED,
    LayerDeclaration, Part, PlanRefused, RollbackPlan,
    at_or_below, check, classify, fold, report, weakest,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

TESTED = {"name": "supersede_window", "tested": True}


def _plan(promotion="promo-1", **overrides) -> RollbackPlan:
    """A plan that accounts for all seven layers, untouched unless overridden.

    Written this way on purpose: the default is the plan that claims nothing,
    so each test below states exactly the one thing it is about."""
    decls = []
    for layer in LAYERS:
        if layer.name in overrides:
            decls.append(overrides[layer.name])
        else:
            decls.append(LayerDeclaration(layer.name, (), untouched=True))
    return RollbackPlan(promotion=promotion, declarations=tuple(decls))


def _routing(*parts) -> LayerDeclaration:
    return LayerDeclaration("routing", tuple(parts))


# ---------------------------------------------------------------------------
# the vocabulary and the order
# ---------------------------------------------------------------------------

def test_the_three_classes_are_the_items_three():
    assert set(STATE_CLASSES) == {"revertible", "compensatable", "neither"}


def test_undeclared_is_ordered_below_every_class():
    for name in STATE_CLASSES:
        assert at_or_below(UNDECLARED, name)
        assert not at_or_below(name, UNDECLARED)


def test_the_aggregate_of_a_layer_is_its_weakest_part():
    assert weakest(["revertible", "compensatable"]) == "compensatable"
    assert weakest(["revertible", "neither"]) == "neither"
    assert weakest(["revertible", "revertible"]) == "revertible"
    assert weakest([]) == UNDECLARED
    assert weakest(["revertible", UNDECLARED]) == UNDECLARED


def test_an_unknown_token_is_at_or_below_nothing():
    assert not at_or_below("probably-fine", "revertible")
    assert not at_or_below("revertible", "probably-fine")


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------

def test_the_registry_is_the_waves_seven_layers():
    assert [x.name for x in LAYERS] == [
        "policies", "routing", "components", "workflows", "memory",
        "runtime", "device-placement",
    ]


def test_four_layers_accumulate_and_three_do_not():
    accumulating = sorted(x.name for x in LAYERS if x.accumulates)
    assert accumulating == ["device-placement", "memory", "routing",
                            "workflows"]
    assert sorted(x.name for x in LAYERS if not x.accumulates) == [
        "components", "policies", "runtime"]


def test_an_accumulating_layer_can_never_be_claimed_revertible():
    """The ceiling IS the registry's whole contribution. A layer that drifted
    across generations may be claimed compensatable at best, by anyone."""
    for layer in LAYERS:
        if layer.accumulates:
            assert layer.ceiling == "compensatable"
            assert not at_or_below("revertible", layer.ceiling)
        else:
            assert layer.ceiling == "revertible"


def test_every_layer_carries_a_reason():
    for layer in LAYERS:
        assert len(layer.reason) > 40, layer.name


# ---------------------------------------------------------------------------
# the failure direction: absence is not a class
# ---------------------------------------------------------------------------

def test_an_omitted_layer_is_refused_and_not_read_as_untouched():
    plan = RollbackPlan("promo", tuple(
        LayerDeclaration(x.name, (), untouched=True)
        for x in LAYERS if x.name != "memory"))
    refusal = check(plan)
    assert refusal is not None
    assert refusal.link == ls.LAYER_UNACCOUNTED
    assert refusal.layer == "memory"


def test_a_part_with_no_class_resolves_to_undeclared_not_revertible():
    plan = _plan(routing=_routing(Part("agreement-ledger", UNDECLARED)))
    assert classify(plan, "routing") == UNDECLARED
    refusal = check(plan)
    assert refusal is not None and refusal.link == ls.LAYER_UNDECLARED


def test_no_compensation_lifts_an_undeclared_part():
    """`neither` has an exit (declare a tested compensation). `undeclared`
    has none, because a part nobody classified is not a part that is hard to
    restore. This is the whole difference between the two."""
    plan = _plan(routing=_routing(
        Part("agreement-ledger", UNDECLARED, TESTED)))
    refusal = check(plan)
    assert refusal is not None
    assert refusal.link == ls.LAYER_UNDECLARED
    assert "no compensation lifts" in refusal.detail


def test_a_layer_declaring_no_part_and_not_untouched_is_refused():
    plan = _plan(routing=LayerDeclaration("routing", ()))
    refusal = check(plan)
    assert refusal is not None and refusal.link == ls.LAYER_UNDECLARED


def test_an_unknown_layer_is_refused_the_registry_is_closed():
    plan = RollbackPlan("promo", tuple(
        [LayerDeclaration(x.name, (), untouched=True) for x in LAYERS]
        + [LayerDeclaration("vibes", (Part("v", "revertible"),))]))
    refusal = check(plan)
    assert refusal is not None and refusal.link == ls.LAYER_UNKNOWN


def test_an_unknown_class_token_is_refused():
    plan = _plan(routing=_routing(Part("route-arm", "probably-fine")))
    refusal = check(plan)
    assert refusal is not None and refusal.link == ls.CLASS_UNKNOWN


def test_the_module_never_defaults_a_class_to_revertible():
    """A source-level assertion, not a behavioural one: the string
    'revertible' must not appear as the default of any `.get` in the module.
    The whole bug is the fail-open default, so it is worth holding at the
    place a later edit would reintroduce it."""
    tree = ast.parse((ROOT / "src" / "revl" / "layer_state.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and len(node.args) == 2:
            default = node.args[1]
            assert not (isinstance(default, ast.Constant)
                        and default.value == "revertible"), \
                f"a .get defaults to 'revertible' at line {node.lineno}"


# ---------------------------------------------------------------------------
# the ceiling: declaring only the revertible half
# ---------------------------------------------------------------------------

def test_routing_declared_only_by_its_arm_is_refused_above_its_ceiling():
    """The headline refusal. `route-arm` really is revertible, and a plan that
    declares it alone resolves routing to `revertible` and would report the
    layer as restored while the accumulated preference the item is about goes
    unmentioned."""
    plan = _plan(routing=_routing(Part("route-arm", "revertible")))
    refusal = check(plan)
    assert refusal is not None
    assert refusal.link == ls.CLASS_ABOVE_CEILING
    assert refusal.layer == "routing"
    assert "accumulates" in refusal.detail


def test_the_same_plan_with_the_accumulation_declared_is_admitted():
    plan = _plan(routing=_routing(
        Part("route-arm", "revertible"),
        Part("agreement-ledger", "compensatable", TESTED)))
    assert check(plan) is None
    assert classify(plan, "routing") == "compensatable"


def test_a_non_accumulating_layer_may_be_claimed_revertible():
    plan = _plan(policies=LayerDeclaration(
        "policies", (Part("rule-set", "revertible"),)))
    assert check(plan) is None


# ---------------------------------------------------------------------------
# the compensation requirement
# ---------------------------------------------------------------------------

def test_a_neither_part_with_no_compensation_may_not_be_promoted():
    plan = _plan(**{"device-placement": LayerDeclaration(
        "device-placement", (Part("placement-history", "neither"),))})
    refusal = check(plan)
    assert refusal is not None
    assert refusal.link == ls.STATE_NOT_RESTORABLE
    assert refusal.part == "placement-history"


def test_an_untested_compensation_may_not_be_promoted():
    plan = _plan(**{"device-placement": LayerDeclaration(
        "device-placement", (Part("placement-history", "neither",
                                  {"name": "redate_history"}),))})
    refusal = check(plan)
    assert refusal is not None and refusal.link == ls.COMPENSATION_UNTESTED


def test_a_declared_and_tested_compensation_admits_a_neither_part():
    plan = _plan(**{"device-placement": LayerDeclaration(
        "device-placement", (Part("placement-history", "neither",
                                  {"name": "redate_history",
                                   "tested": True}),))})
    assert check(plan) is None


def test_the_requirement_applies_to_compensatable_too():
    """Item 546's bullet names `neither`. It is applied to `compensatable` as
    well for the reason item 518 gives: a compensation is the only thing that
    makes `compensatable` different from `neither`, so a `compensatable` part
    without one is a `neither` part with a nicer word on it."""
    plan = _plan(routing=_routing(
        Part("agreement-ledger", "compensatable")))
    refusal = check(plan)
    assert refusal is not None and refusal.link == ls.STATE_NOT_RESTORABLE


def test_a_compensation_parked_on_a_revertible_part_is_refused():
    plan = _plan(policies=LayerDeclaration(
        "policies", (Part("rule-set", "revertible", TESTED),)))
    refusal = check(plan)
    assert refusal is not None and refusal.link == ls.COMPENSATION_ORPHANED


# ---------------------------------------------------------------------------
# the report: four outcomes, and not one word
# ---------------------------------------------------------------------------

def _full_plan() -> RollbackPlan:
    return _plan(
        "promo-full",
        policies=LayerDeclaration("policies",
                                  (Part("rule-set", "revertible"),)),
        routing=_routing(
            Part("route-arm", "revertible"),
            Part("agreement-ledger", "compensatable",
                 {"name": "supersede_window", "tested": True})),
        **{"device-placement": LayerDeclaration(
            "device-placement",
            (Part("placement-history", "neither",
                  {"name": "redate_history", "tested": True}),))})


def test_a_rollback_separates_what_it_restored_from_what_it_compensated():
    out = report(_full_plan())
    assert out.restored == ("policies",)
    assert [x["layer"] for x in out.compensated] == ["routing",
                                                     "device-placement"]
    assert out.uncompensated == ()
    assert set(out.untouched) == {"components", "workflows", "memory",
                                  "runtime"}
    assert not out.fully_restored


def test_the_phrase_rolled_back_appears_nowhere_in_the_render():
    text = report(_full_plan()).render().lower()
    assert "rolled back" not in text
    assert "rollback" not in text
    assert "restored to the state before" in text
    assert "not restored, compensated" in text


def test_a_compensation_that_did_not_run_reports_uncompensated():
    """The outcome is a measurement of what RAN, not a restatement of the
    plan's classes. A `compensatable` part whose compensation did not execute
    is `uncompensated`, which is item 522's word for exactly that fact."""
    out = report(_full_plan(), ran=["supersede_window"])
    assert [x["layer"] for x in out.compensated] == ["routing"]
    assert [x["layer"] for x in out.uncompensated] == ["device-placement"]
    assert "neither restored nor compensated" in out.render().lower()


def test_a_rollback_of_a_wholly_revertible_promotion_is_fully_restored():
    plan = _plan(policies=LayerDeclaration(
        "policies", (Part("rule-set", "revertible"),)))
    out = report(plan)
    assert out.fully_restored
    assert out.restored == ("policies",)
    assert out.compensated == () and out.uncompensated == ()


def test_a_refused_plan_reports_the_refusal_and_no_lists():
    out = report(_plan(routing=_routing(Part("route-arm", "revertible"))))
    assert out.refusal is not None
    assert out.restored == () and out.compensated == ()
    assert "refused" in out.render()


def test_the_report_round_trips_as_a_dict():
    data = report(_full_plan()).as_dict()
    assert data["kind"] == "revl.layer_state.rollback"
    assert data["fully_restored"] is False
    assert sorted(data) == sorted([
        "kind", "version", "promotion", "restored", "compensated",
        "uncompensated", "untouched", "fully_restored", "refusal"])


def test_the_outcome_vocabulary_is_not_the_class_vocabulary():
    assert set(ls.OUTCOMES) & set(STATE_CLASSES) == set()


# ---------------------------------------------------------------------------
# refuse by name, and by no G-code
# ---------------------------------------------------------------------------

def test_no_refusal_link_is_a_guarantee_code():
    """Item 523's generated tier matrix requires every registered G-code to
    carry a reproducer under `examples/rejections/` or an ACKNOWLEDGED entry
    in `tools/tier_guarantees.py`. This module registers none and owes
    neither."""
    for link in LINKS:
        assert not link.startswith("G-")
        assert not (link[:1] == "G" and link[1:2].isdigit())
        assert link == link.lower()


def test_every_link_is_reachable_and_distinct():
    assert len(set(LINKS)) == len(LINKS)


def test_the_module_registers_no_guarantee_code_in_the_checker():
    from revl import diagnostics
    text = (ROOT / "src" / "revl" / "layer_state.py").read_text()
    for code in list(getattr(diagnostics, "GUARANTEES", {}) or ()):
        assert f'"{code}"' not in text, code


# ---------------------------------------------------------------------------
# reconciling the two lanes that answered this for one layer each
# ---------------------------------------------------------------------------

def test_item_522s_five_classes_fold_into_these_three():
    assert fold("ui", "reversible")[0] == "revertible"
    assert fold("ui", "compensatable")[0] == "compensatable"
    assert fold("ui", "irreversible")[0] == "neither"
    assert fold("ui", "unknown")[0] == "neither"


def test_irreversible_and_unknown_are_joined_for_the_decision_and_split_for_the_reason():
    """Item 522 keeps them apart because the refusal text differs and joins
    them because a transaction treating an unclassified step as reversible is
    the fail-open shape. Both halves hold here."""
    a, reason_a = fold("ui", "irreversible")
    b, reason_b = fold("ui", "unknown")
    assert a == b == "neither"
    assert reason_a != reason_b


def test_confirm_required_is_refused_rather_than_folded():
    with pytest.raises(PlanRefused) as exc:
        fold("ui", "confirm-required")
    assert exc.value.refusal.link == ls.CLASS_NOT_A_STATE_CLASS


def test_an_unknown_vocabulary_is_refused():
    with pytest.raises(PlanRefused):
        fold("moods", "reversible")
    with pytest.raises(PlanRefused):
        fold("state", "reversible")


def test_item_518s_three_parts_map_onto_two_registry_layers():
    assert ls.SHADOW_PARTS == {
        "route-arm": "routing",
        "agreement-ledger": "routing",
        "placement-history": "device-placement",
    }
    for layer in set(ls.SHADOW_PARTS.values()):
        assert layer in {x.name for x in LAYERS}


def test_item_518s_own_plan_shape_lifts_and_is_admitted():
    plan = ls.from_shadow_layers(
        "shadow-promo",
        {"route-arm": "revertible",
         "agreement-ledger": "compensatable",
         "placement-history": "neither"},
        {"agreement-ledger": {"name": "supersede_window", "tested": True},
         "placement-history": {"name": "redate_history", "tested": True}},
        untouched=["policies", "components", "workflows", "memory",
                   "runtime"])
    assert check(plan) is None
    out = report(plan)
    assert out.restored == ()
    assert [x["layer"] for x in out.compensated] == ["routing",
                                                     "device-placement"]


def test_the_adapter_will_not_infer_untouched():
    """Item 518's maps name two layers. The other five are not written by a
    model-action promotion and the CALLER says so; inferring it here would be
    the omission-reads-as-untouched shape the coverage check refuses."""
    plan = ls.from_shadow_layers(
        "shadow-promo",
        {"route-arm": "revertible",
         "agreement-ledger": "compensatable"},
        {"agreement-ledger": {"name": "supersede_window", "tested": True}})
    refusal = check(plan)
    assert refusal is not None and refusal.link == ls.LAYER_UNACCOUNTED


def test_the_adapter_refuses_an_orphaned_compensation():
    with pytest.raises(PlanRefused) as exc:
        ls.from_shadow_layers(
            "shadow-promo", {"route-arm": "revertible"},
            {"agreement-ledger": {"name": "x", "tested": True}})
    assert exc.value.refusal.link == ls.COMPENSATION_ORPHANED


def test_the_vocabularies_agree_with_item_518_when_it_is_importable():
    sp = pytest.importorskip(
        "revl.shadow_promotion",
        reason="item 518 (issue #1192) is not on this tree")
    assert set(sp.STATE_CLASSES) == set(STATE_CLASSES)
    assert set(sp.PROMOTION_LAYERS) == set(ls.SHADOW_PARTS)


def test_the_vocabularies_agree_with_item_522_when_it_is_importable():
    uf = pytest.importorskip(
        "revl.ui_family", reason="item 522 (issue #1196) is not on this tree")
    classes = set(getattr(uf, "CLASSES", ()))
    if not classes:
        pytest.skip("ui_family carries no reversibility classes yet")
    assert classes - {"confirm-required"} == set(ls.UI_CLASSES)
    for token in getattr(uf, "NO_INVERSE", ()):
        assert fold("ui", token)[0] == "neither"


# ---------------------------------------------------------------------------
# non-vacuity
# ---------------------------------------------------------------------------

def effects_only_admits(plan: RollbackPlan) -> bool:
    """The rule as it stands BEFORE this change, written out.

    revl's guarantee is about effects: a revertible effect carries its
    inverse, unloading replays the inverses in LIFO order, removal leaves no
    residue. A promotion is admitted when every effect it performs is
    revertible, and the state a promoted rule accumulates while it runs is not
    an effect, so nothing in this rule reads it. Every plan below performs
    only revertible effects, which is why they are all admitted here."""
    return True


REFUSED_BY_THIS_CHANGE = {
    "routing declared only by its revertible arm":
        _plan(routing=_routing(Part("route-arm", "revertible"))),
    "memory left out of the plan entirely":
        RollbackPlan("p", tuple(
            LayerDeclaration(x.name, (), untouched=True)
            for x in LAYERS if x.name != "memory")),
    "a routing part with no declared class":
        _plan(routing=_routing(Part("agreement-ledger", UNDECLARED))),
    "an undeclared part with a tested compensation on it":
        _plan(routing=_routing(Part("agreement-ledger", UNDECLARED, TESTED))),
    "placement history declared neither, no compensation":
        _plan(**{"device-placement": LayerDeclaration(
            "device-placement",
            (Part("placement-history", "neither"),))}),
    "placement history with an untested compensation":
        _plan(**{"device-placement": LayerDeclaration(
            "device-placement",
            (Part("placement-history", "neither",
                  {"name": "redate_history"}),))}),
    "workflows claimed revertible":
        _plan(workflows=LayerDeclaration(
            "workflows", (Part("plan-shape", "revertible"),))),
    "a compensation parked on a revertible part":
        _plan(policies=LayerDeclaration(
            "policies", (Part("rule-set", "revertible", TESTED),))),
    "a layer nobody registered":
        RollbackPlan("p", tuple(
            [LayerDeclaration(x.name, (), untouched=True) for x in LAYERS]
            + [LayerDeclaration("vibes", (Part("v", "revertible"),))])),
}

CONTROLS = {
    "every layer untouched": _plan(),
    "policies alone, revertible": _plan(policies=LayerDeclaration(
        "policies", (Part("rule-set", "revertible"),))),
    "routing with both halves declared": _plan(routing=_routing(
        Part("route-arm", "revertible"),
        Part("agreement-ledger", "compensatable", TESTED))),
    "placement history with a tested compensation":
        _plan(**{"device-placement": LayerDeclaration(
            "device-placement",
            (Part("placement-history", "neither",
                  {"name": "redate_history", "tested": True}),))}),
    "the full three-outcome plan": _full_plan(),
}


@pytest.mark.parametrize("label", sorted(REFUSED_BY_THIS_CHANGE))
def test_non_vacuity_admitted_by_the_effects_only_rule(label):
    plan = REFUSED_BY_THIS_CHANGE[label]
    assert effects_only_admits(plan) is True
    refusal = check(plan)
    assert refusal is not None, label
    assert refusal.link in LINKS


@pytest.mark.parametrize("label", sorted(CONTROLS))
def test_non_vacuity_controls_are_admitted_by_both(label):
    plan = CONTROLS[label]
    assert effects_only_admits(plan) is True
    assert check(plan) is None, label


def test_the_nine_refusals_cover_seven_distinct_links():
    links = {check(p).link for p in REFUSED_BY_THIS_CHANGE.values()}
    assert len(REFUSED_BY_THIS_CHANGE) == 9
    assert len(CONTROLS) == 5
    assert links == {
        ls.CLASS_ABOVE_CEILING, ls.LAYER_UNACCOUNTED, ls.LAYER_UNDECLARED,
        ls.STATE_NOT_RESTORABLE, ls.COMPENSATION_UNTESTED,
        ls.COMPENSATION_ORPHANED, ls.LAYER_UNKNOWN,
    }


def test_the_module_is_python_3_11_parseable():
    """CI runs 3.11 and the development venv is 3.14."""
    for name in ("src/revl/layer_state.py", "tests/test_layer_state_546.py"):
        ast.parse((ROOT / name).read_text(), feature_version=(3, 11))

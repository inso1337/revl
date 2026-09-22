"""The crossing side of the model placement: roadmap item 512, slice 4.

`docs/design/531-model-placement.md` section 8 names this slice and
`docs/design/554-route-model-remaining.md` is where it landed. Slice 1 checks
which roles an action MAY reach; this checks which it DOES.

The gap between the two is the one section 9 of the first note hands to item
515: "a scheduler that picks a role no arm names is the fail-open shape, and S4
is what makes that refusable". A `route model` block that a later item can
widen from the outside is not a permission, it is a suggestion with a syntax.

WHY THESE PROGRAMS ARE INLINE STRINGS. `examples/rejections/` and
`tests/fixtures/` are both corpus roots for `tools/gate_reference_census.py`,
and this rule reads a capability token and a flow position - neither of which
the self-host gate has. A rejection fixture here would be a program the
reference refuses and the gate raises no objection to, which is a
`no-objection-out-of-slice` row rather than a defect, but it would also make
the `G-MODEL-PLACE` row of `docs/conformance.md` read `divergence` on the revl
tier for a reason that is not a divergence in the rule. Slice 3's
DECLARATION-half fixture is in the corpus precisely because the gate decides
it. This one is not, for the mirror-image reason, and the moment a self-host
port of the crossing side exists it should move.

CORPUS DISCIPLINE. Every refusing program here has exactly one reference
refusal and it is this one; every admitting program compiles.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl import model_route  # noqa: E402


ROLES = "model role local on_device\nmodel role edge on_device\n"


def _program(cap: str, arms: str, roles: str = ROLES, action: str = "classify",
             block: bool = True) -> str:
    clause = f"  route model on {action} {{ {arms} }}\n" if block else ""
    return (
        f"{roles}"
        f"extern emission[{cap}] fn ask(p: Str) -> Int = @py {{ return 0 }}\n"
        "service Answer { emission fn classify(text: Str) -> Int }\n"
        "component Classifier provides out: Answer {\n"
        f"{clause}"
        "  provide out {\n"
        "    fn classify(text) {\n"
        "      let r = ask(text)\n"
        "      return r\n"
        "    }\n"
        "  }\n"
        "}\n")


# ------------------------------------------------------------ the refusal

def test_a_crossing_on_a_role_no_arm_names_is_refused():
    with pytest.raises(RevlError) as excinfo:
        compile_source(_program("model.edge", "web -> local"), "reach.rvl")
    error = excinfo.value
    assert error.code == "G-MODEL-PLACE"
    # the diagnostic names the action, the role and what the block DOES reach,
    # which is the shape item 512's exit asks of every refusal on this surface.
    assert "action `classify`" in error.message
    assert "model role `edge`" in error.message
    assert "the block reaches `local`" in error.message


def test_the_refusal_names_every_role_the_block_reaches():
    with pytest.raises(RevlError) as excinfo:
        compile_source(
            _program("model.edge", "web -> local, net -> local, * -> local"),
            "reach.rvl")
    assert "the block reaches `local`" in excinfo.value.message


def test_a_block_with_a_star_arm_does_not_reach_every_role():
    """`*` widens the ORIGINS an arm covers, never the ROLES a block names.

    This is the catch-all's second reading and it matters as much as the first:
    if `* -> local` meant "any origin to any role" the block would place
    nothing, exactly as `* -> cloud` would place a confidential input if `*`
    covered a confidentiality origin.
    """
    with pytest.raises(RevlError) as excinfo:
        compile_source(_program("model.edge", "* -> local"), "reach.rvl")
    assert "model role `edge`" in excinfo.value.message


# ------------------------------------------------------------ the admissions

def test_a_crossing_on_a_role_an_arm_names_admits():
    assert compile_source(_program("model.local", "web -> local"), "reach.rvl")


def test_a_crossing_on_a_role_named_by_any_arm_admits():
    """The reach is the block's whole role set, not one arm's."""
    assert compile_source(
        _program("model.edge", "web -> local, net -> edge"), "reach.rvl")


def test_an_operation_token_is_not_a_placement():
    """`model.complete` is item 343's OPERATION token and stays one.

    The role reading is opt-in: a tail names a role only when a role by that
    name was declared. Every shipped `model.*` crossing keeps compiling, which
    is the property that makes this slice additive rather than a migration.
    """
    assert compile_source(_program("model.complete", "web -> local"),
                          "reach.rvl")


def test_a_program_that_declares_no_role_is_untouched():
    assert compile_source(
        _program("model.edge", "", roles="", block=False), "reach.rvl")


def test_an_action_with_no_block_is_untouched():
    """The same line `admits()` draws for an unrouted component.

    A program that declared no placement for this action is judged by the
    rules that judged it before item 512. Making `route model` mandatory would
    be a different item, and this one adds refusals rather than obligations.
    """
    assert compile_source(_program("model.edge", "", block=False),
                          "reach.rvl")


def test_a_sibling_action_block_does_not_place_this_one():
    """A component that routes `summarize` and not `classify` leaves
    `classify` unrouted for THIS rule, which is the `route_arms is None` line.

    Item 514 refuses a confidential VALUE in that position (`unrouted`); the
    crossing side does not, because a crossing carrying no confidentiality
    origin into an action nobody placed is the pre-512 state of the world.
    """
    src = (
        f"{ROLES}"
        "extern emission[model.edge] fn ask(p: Str) -> Int = @py { return 0 }\n"
        "service Answer {\n"
        "  emission fn classify(text: Str) -> Int\n"
        "  emission fn summarize(doc: Str) -> Int\n"
        "}\n"
        "component Classifier provides out: Answer {\n"
        "  route model on summarize { web -> local }\n"
        "  provide out {\n"
        "    fn classify(text) { let r = ask(text) return r }\n"
        "    fn summarize(doc) { return 0 }\n"
        "  }\n"
        "}\n")
    assert compile_source(src, "reach.rvl")


# ------------------------------------------------------- the unit of the rule

def test_role_of_crossing_reads_a_declared_role_only():
    roles = {"local": object(), "edge": object()}
    assert model_route.role_of_crossing("model.local", roles) == "local"
    assert model_route.role_of_crossing("model.complete", roles) is None
    assert model_route.role_of_crossing("fs.read", roles) is None
    assert model_route.role_of_crossing("model", roles) is None
    assert model_route.role_of_crossing(None, roles) is None
    assert model_route.role_of_crossing("model.local", {}) is None


def test_role_of_crossing_reads_a_parameterised_capability_by_its_head():
    """item 294: `model.local(calls=3)` is the same placement as `model.local`."""
    assert model_route.role_of_crossing(
        "model.local(calls=3)", {"local": object()}) == "local"


def test_reach_of_reads_every_arm():
    arms = {"web": {"role": "local", "residence": "on_device"},
            "net": {"role": "edge", "residence": "on_device"}}
    assert model_route.reach_of(arms) == frozenset({"local", "edge"})
    assert model_route.reach_of(None) == frozenset()
    assert model_route.reach_of({}) == frozenset()


def test_reach_of_reads_a_candidate_set_when_a_placement_carries_one():
    """The one line that has to move when item 515's `a | b | c` lands.

    No placement carries `candidates` today. Reading the key now means the
    reach widens with that surface the moment `check()` records it, rather
    than this rule refusing a fallback the program plainly names - which would
    be the opposite of the fail-closed direction, an over-refusal of a written
    intent.
    """
    arms = {"web": {"role": "local", "residence": "on_device",
                    "candidates": ("local", "edge")}}
    assert model_route.reach_of(arms) == frozenset({"local", "edge"})


def test_the_rule_is_the_one_that_makes_a_scheduler_unable_to_widen():
    """Item 515's inheritance note, as an executable sentence.

    A portfolio that schedules inside this boundary may reorder, may fall back
    and may decline; what it may not do is run the action on a role the program
    never named. The refusal above is what makes that unrepresentable rather
    than merely discouraged.
    """
    with pytest.raises(RevlError) as excinfo:
        compile_source(_program("model.edge", "confidential -> local"),
                       "reach.rvl")
    assert excinfo.value.code == "G-MODEL-PLACE"
    assert "widens the placement" in (excinfo.value.hint or "")

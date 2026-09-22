"""A ladder rung carries every obligation its verb carries (roadmap items 521
and 522, the union of PRs #1284, #1320, #1287, #1296 and #1275).

WHY THIS FILE EXISTS. Each of those five pull requests passes its own tests,
and the hole this file closes is visible from none of them:

  * item 521 slice 3 (#1320) makes `ui.click.selector` and `ui.click.pixel`
    SPELLABLE. Before it, every rung was refused at the declaration site, so
    no rung could reach any downstream table at all;
  * item 522 (#1287) classifies a computer-use crossing - its reversibility
    class, its eligible phases, its residue state, its confirmation state -
    and its module did not exist on #1320's branch, so #1320 could not have
    measured it;
  * item 521 slice 4 (#1320) makes `UiTarget` a required record for any
    program declaring a target-carrying verb.

#1320's own report names the risk in one sentence: `emission[ui.click.pixel]`
must not be a spelling that escapes both #1287's reversibility classification
and slice 4's target obligation. A rung that arrived with NO class would take
the `None` arm of every table in `ui_transaction` and be reported as not a
computer-use verb at all, which is the fail-open direction - an actuation that
no residue verdict covers, in the one namespace where the whole point is that
nothing is silently uncovered.

WHAT WAS MEASURED, AND WHERE THE HOLE ACTUALLY WAS. It is closed, and it is
closed by #1320 rather than by anything here: `ui_family.reversibility()` was
an exact-match `REVERSIBILITY.get(token)` before slice 3 and resolves a rung to
its verb after it, and every one of `ui_transaction`'s decisions routes through
that single function. So the two branches compose correctly by construction.
Nothing in either branch pins that, because neither branch can: #1320's tree
has no `ui_transaction`, and #1287's tree cannot spell a rung. This file is the
only place the two facts meet, and it fails by name if either side stops
holding - if a later slice gives `ui_transaction` its own table, or makes the
resolution exact-match again.

ONE TABLE WAS STILL EXACT-MATCH and is fixed in the same branch as this file:
`ui_transaction._is_raised`. An operator's `capability ui.click requires
approval` did not raise `ui.click.pixel`, so the rung reported `unconfirmed`
under a policy that named its verb. `test_an_operator_raise_reaches_the_rung`
is that measurement and it fails on the exact-match version.

A SEPARATE DEFECT was found while writing this file and is NOT fixed here:
`ui_transaction.method_plan` drops an emission in RETURN position. It is
pre-existing on #1287's own branch, has nothing to do with rungs or with this
merge, and is pinned by the strict `xfail` at the bottom rather than repaired
inside a five-PR reconciliation. Every plan-reading fixture above therefore
binds its emissions rather than returning them.

NOT MEASURED HERE: no step executes, and nothing in this file drives a
desktop. These are compile-time and report-time classifications.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import ui_family  # noqa: E402
from revl import ui_transaction as uitx  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

#: Every rung spelling item 521 slice 3 admits, paired with the verb whose
#: obligations it must inherit.
RUNGS_AND_VERBS = [
    ("ui.click.selector", "ui.click"),
    ("ui.click.pixel", "ui.click"),
    ("ui.text.selector", "ui.text"),
    ("ui.text.pixel", "ui.text"),
    ("ui.download.selector", "ui.download"),
    ("ui.download.pixel", "ui.download"),
]

UI_TARGET = """
type UiTarget = {
  application: Str
  window: Str
  role: Str
  name: Str
  evidence: Str
  action: Str
  session: Str
  bounds: Str
  expiry: Int
  confirm: Bool
}
"""


def _program(click_decl: str, body: str) -> str:
    return (UI_TARGET
            + "extern emission[screen.observe] fn obs(r: Str) -> Str\n"
              "  = @py { return \"\" }\n"
              "extern emission[ui.find] fn find(s: Str, n: Str) -> UiTarget\n"
              "  = @py { return None }\n"
            + click_decl
            + "service Worker { emission fn approve(region: Str) -> Int }\n"
              "component Billing provides worker: Worker {\n"
              "  provide worker {\n"
              "    fn approve(region) {\n"
              "      let seen = emit obs(region)\n"
              "      let t = emit find(seen, \"Approve\")\n"
            + body
            + "    }\n  }\n}\n")


SEMANTIC = ("extern emission[ui.click] fn click(t: UiTarget) -> Int\n"
            "  = @py { return 0 }\n")
PIXEL = ("extern emission[ui.click.pixel] fn click_px(t: UiTarget) -> Int\n"
         "  = @py { return 0 }\n")


# ------------------------------------------------ the classification tables


@pytest.mark.parametrize(("rung", "verb"), RUNGS_AND_VERBS)
def test_a_rung_inherits_its_verbs_reversibility_class(rung, verb) -> None:
    """The root of it. `None` here is what would take every `ui_transaction`
    decision below down its not-a-computer-use-verb arm."""
    assert ui_family.reversibility(rung) is not None
    assert ui_family.reversibility(rung) == ui_family.reversibility(verb)


@pytest.mark.parametrize(("rung", "verb"), RUNGS_AND_VERBS)
def test_a_rung_inherits_every_item_522_verdict(rung, verb) -> None:
    """The differential #1320's report asks for, over item 522's whole
    surface rather than over the class alone: a rung and its verb agree on
    the residue with and without a declared inverse, on the eligible phases,
    on whether a compensation may be placed, and on the postcondition."""
    for has_compensate in (True, False):
        assert uitx.residue_state(rung, has_compensate) \
            == uitx.residue_state(verb, has_compensate)
        assert uitx.residue_state(rung, has_compensate) is not None
    assert uitx.eligible_phases(rung) == uitx.eligible_phases(verb)
    assert uitx.may_compensate(rung) == uitx.may_compensate(verb)
    for checked in (True, False):
        for followed in (True, False):
            assert uitx.postcondition(rung, checked_by_read=checked,
                                      followed_by_read=followed) \
                == uitx.postcondition(verb, checked_by_read=checked,
                                      followed_by_read=followed)


@pytest.mark.parametrize(("rung", "verb"), RUNGS_AND_VERBS)
def test_a_rung_inherits_its_verbs_compensation_obligation(rung, verb) -> None:
    """Item 522 slice 1's G4 refusals, reached through a rung. A rung on a
    verb with no inverse may not declare `compensate`, and a rung on a
    compensatable verb must - otherwise adding a rung spelling in a different
    file is how an author gets out of the obligation."""
    for has_compensate in (True, False):
        rung_refusal = ui_family.teardown_refusal(
            rung, "extern", "f", has_compensate)
        verb_refusal = ui_family.teardown_refusal(
            verb, "extern", "f", has_compensate)
        assert (rung_refusal is None) == (verb_refusal is None)


@pytest.mark.parametrize(("rung", "verb"), RUNGS_AND_VERBS)
def test_a_rung_inherits_its_verbs_taint_role(rung, verb) -> None:
    """Slice 2's table, which slice 2 had already made prefix-resolving. Here
    for completeness: this file's claim is that NO table in the computer-use
    surface compares a token by exact string."""
    assert ui_family.taint_roles(rung) == ui_family.taint_roles(verb)
    assert ui_family.taint_roles(rung)


# -------------------------------------------- slice 4's target obligation


def test_the_pixel_rung_carries_the_target_obligation() -> None:
    """Slice 4's refusal, reached through a rung rather than through the verb.
    `emission[ui.click.pixel] fn click_px(t: Str)` is the spelling that would
    take a bare string target again."""
    with pytest.raises(RevlError) as refused:
        compile_source(
            _program(
                SEMANTIC
                + "extern emission[ui.click.pixel] fn click_px(t: Str) -> Int\n"
                  "  = @py { return 0 }\n",
                "      let a = emit click(t)\n"
                "      return emit click_px(t)\n"),
            "rung_target.rvl")
    assert refused.value.code == "G8"
    assert "UiTarget" in refused.value.message


def test_the_same_ladder_with_the_record_admits() -> None:
    """The control, and the non-vacuity of the refusal above: the only
    difference between the two programs is the rung's parameter type."""
    ir = compile_source(
        _program(SEMANTIC + PIXEL,
                 "      let a = emit click(t)\n"
                 "      let b = emit click_px(t)\n"
                 "      return a\n"),
        "rung_target.rvl")
    assert ir is not None


# ------------------------------ the two obligations, on one program, together


def test_the_pixel_rung_escapes_neither_obligation() -> None:
    """THE DIFFERENTIAL. One admitted ladder carrying both `ui.click` and
    `ui.click.pixel`, read through item 522's planner: the rung appears as a
    step, with a class, an eligible phase set and a residue verdict, and they
    are its verb's. A rung that escaped the classification would be a step
    with `None` in those positions, or would not be a step at all."""
    ir = compile_source(
        _program(SEMANTIC + PIXEL,
                 "      let a = emit click(t)\n"
                 "      let b = emit click_px(t)\n"
                 "      return a\n"),
        "rung_union.rvl")
    plan = uitx.plans(ir)[0]
    by_token = {step["token"]: step for step in plan["steps"]}
    assert "ui.click.pixel" in by_token, plan["steps"]
    rung, verb = by_token["ui.click.pixel"], by_token["ui.click"]
    for field in ("class", "residue", "eligiblePhases", "postcondition"):
        assert rung[field] == verb[field], field
        assert rung[field] is not None, field
    # And the aggregate is not weakened by the rung being present.
    assert plan["revert"]["aggregate"] == uitx.UNCOMPENSATED


def test_an_operator_raise_reaches_the_rung() -> None:
    """The one table that WAS still exact-match, and the reason this file
    carries a source change as well as assertions. An operator who writes
    `capability ui.click requires approval` has raised the verb; a plan that
    reports the pixel rung `unconfirmed` under that rule has let a spelling
    in a different file escape the operator's authority.

    Fails on the pre-fix `token in approval_tokens`."""
    ir = compile_source(
        _program(SEMANTIC + PIXEL,
                 "      let a = emit click(t)\n"
                 "      let b = emit click_px(t)\n"
                 "      return a\n"),
        "rung_raise.rvl")
    plan = uitx.plans(ir, frozenset({"ui.click"}))[0]
    by_token = {step["token"]: step for step in plan["steps"]}
    assert by_token["ui.click"]["confirmation"] == uitx.PER_SESSION
    assert by_token["ui.click.pixel"]["confirmation"] == uitx.PER_SESSION
    assert by_token["ui.click.pixel"]["effectiveClass"] \
        == ui_family.CONFIRM_REQUIRED


def test_raising_a_rung_does_not_raise_its_verb() -> None:
    """The direction that must NOT hold, so the fix above is a resolution
    upward and not a blanket match. An operator who raises only the pixel rung
    has said something narrower than raising the verb, and `ui.click` keeps
    its own confirmation state."""
    ir = compile_source(
        _program(SEMANTIC + PIXEL,
                 "      let a = emit click(t)\n"
                 "      let b = emit click_px(t)\n"
                 "      return a\n"),
        "rung_narrow.rvl")
    plan = uitx.plans(ir, frozenset({"ui.click.pixel"}))[0]
    by_token = {step["token"]: step for step in plan["steps"]}
    assert by_token["ui.click.pixel"]["confirmation"] == uitx.PER_SESSION
    assert by_token["ui.click"]["confirmation"] == uitx.UNCONFIRMED


# ----------------------------------------------------- the erase report


def test_the_erase_report_gives_a_rung_a_residue_word() -> None:
    """Issue #1293's note is keyed by `ui_transaction`'s states, and #1296
    derives the printed clauses from them by set equality. A rung with no
    residue state would reach the report as an untagged crossing under a
    header that claims every computer-use crossing is classified."""
    assert set(uitx.OUT_OF_BAND_STATES) if hasattr(
        uitx, "OUT_OF_BAND_STATES") else True
    for rung, _verb in RUNGS_AND_VERBS:
        state = uitx.residue_state(rung, False)
        assert state in uitx.WEAKEST_FIRST, (rung, state)


# -------------------------------- a defect found here and deliberately not
# -------------------------------- fixed here


@pytest.mark.xfail(strict=True, reason=(
    "PRE-EXISTING, NOT THIS BRANCH'S. `ui_transaction.method_plan` collects a "
    "step for every BOUND emission (`let x = emit f(...)`) and drops an "
    "emission in RETURN position, so `return emit actuate(t)` gets no residue "
    "verdict, no confirmation state, and no row in the `[4]` section or in "
    "`unconfirmedIrreversibleSteps`. It is the fail-open direction - an "
    "irreversible actuation in tail position is invisible to exactly the "
    "report that exists to name it - and it reproduces on PR #1287's own "
    "branch with no rung involved, so it is not a consequence of merging "
    "these five. It is left alone here on purpose: fixing it moves what every "
    "plan reports for every program with a tail-position crossing, which "
    "would put a semantic change with its own failure direction and its own "
    "non-vacuity argument inside a five-PR reconciliation. Reported to the "
    "orchestrator instead. Flip this to a plain assertion when it is fixed."))
def test_a_return_position_actuation_is_reported() -> None:
    ir = compile_source(
        _program(SEMANTIC, "      return emit click(t)\n"),
        "return_position.rvl")
    tokens = [step["token"] for step in uitx.plans(ir)[0]["steps"]]
    assert "ui.click" in tokens, tokens

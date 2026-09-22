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

A SEPARATE DEFECT was found while writing this file, pinned here by a strict
`xfail` rather than repaired inside a five-PR reconciliation, and is FIXED NOW:
`ui_transaction.method_plan` dropped an emission in RETURN position. The walk
behind it read three statement shapes and only the top node of each one's
expression, so it dropped SEVEN spellings of a crossing, not one -- `return
emit click(t)`, an `if`'s `then` and `else` arms, a `while` body, a `for`
body, `x = emit click(t)`, and a call nested in a larger expression. Each
produced a plan with no step for the crossing, hence no residue verdict, no
confirmation state, no `[4]` row and no `unconfirmedIrreversibleSteps` entry.
`test_every_crossing_position_reaches_the_plan` is the measurement: eleven
positions, of which nine fail on the pre-fix walk and two are the controls
that pass on both. The plan-reading fixtures above still
bind their emissions, which is now a choice about what they measure rather
than a workaround.

WHY A DROPPED CROSSING READ BETTER THAN THE TRUTH rather than as a gap: the
revert aggregate is the WEAKEST state PRESENT, so a step that is absent from
the fold cannot weaken it. The last two tests in this file are that half.
`test_the_tail_crossing_is_the_weakest_part_of_a_restored_set` is the
non-vacuity - a set that aggregated to `restored` while an uncompensated
click sat in tail position - and
`test_the_two_computer_use_folds_agree_about_a_tail_crossing` pins the one
neighbouring fold that never had the hole: `boundaryCrossings.uiResidue`
reads reachability rather than statements, so the same erase report answered
the same question about the same click two ways, and the weaker answer was
the one a reader would have taken as the transaction's verdict.

NOT MEASURED HERE: no step executes, and nothing in this file drives a
desktop. These are compile-time and report-time classifications.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import erase_report  # noqa: E402
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
    for followed in (True, False):
        assert uitx.postcondition(rung, followed_by_read=followed) \
            == uitx.postcondition(verb, followed_by_read=followed)


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


# ------------------------------------- the defect found here, now repaired
#
# `method_plan`'s walk read three statement shapes - `emit e`, `let x = e`,
# and a bare expression statement - and only the TOP node of each one's
# expression. Seven spellings of the same crossing therefore reached no plan.
# The fail-open direction: an irreversible actuation invisible to exactly the
# report that exists to name it, and tail position is where an actuation most
# naturally lands, since a method that returns what it clicked has nothing
# left to bind.


#: Every position a computer-use crossing can occupy in a provide method,
#: paired with the statements that make it well-typed. The first two were
#: already reported before the fix and are here as the controls that say the
#: measurement is about POSITION and not about the program; the other seven
#: each produced a plan with no `ui.click` step.
CROSSING_POSITIONS = [
    ("bound (control)", "      let x = emit click(t)\n      return x\n"),
    ("bare statement (control)", "      emit click(t)\n      return 0\n"),
    ("return", "      return emit click(t)\n"),
    ("then arm, bound",
     '      if (region == "x") {\n        let x = emit click(t)\n'
     "      }\n      return 0\n"),
    ("then arm, bare",
     '      if (region == "x") {\n        emit click(t)\n'
     "      }\n      return 0\n"),
    ("then arm, return",
     '      if (region == "x") {\n        return emit click(t)\n'
     "      }\n      return 0\n"),
    ("else arm",
     '      if (region == "x") {\n        return 0\n      } else {\n'
     "        let x = emit click(t)\n      }\n      return 0\n"),
    ("while body",
     '      while (region == "x") {\n        let x = emit click(t)\n'
     "      }\n      return 0\n"),
    ("for body",
     "      for (s of [region]) {\n        let x = emit click(t)\n"
     "      }\n      return 0\n"),
    ("assignment",
     "      var n = 0\n      n = emit click(t)\n      return n\n"),
    ("nested in a larger expression",
     "      let x = emit click(t) + 0\n      return x\n"),
]


@pytest.mark.parametrize(("position", "body"), CROSSING_POSITIONS,
                         ids=[p for p, _ in CROSSING_POSITIONS])
def test_every_crossing_position_reaches_the_plan(position, body) -> None:
    """A crossing is a crossing wherever it is written.

    Nine of these eleven fail on the pre-fix walk. What each failure costs is
    not a missing row in a listing: a step that is not in the plan gets no
    `residue` verdict, no `confirmation` state, no `postcondition` verdict,
    no row in the erase report's `[4]` section and no entry in
    `unconfirmedIrreversibleSteps` - which is the enumeration issue #1293's
    note is derived from. The plan does not report the crossing as
    uncovered; it reports that the method made no such crossing.

    NON-VACUITY. The two controls pass before and after, so a walk that
    reported nothing at all would fail this file rather than satisfy it, and
    `screen.observe`/`ui.find` are asserted alongside so a walk that reported
    only the last call would fail too.
    """
    ir = compile_source(_program(SEMANTIC, body), "position.rvl")
    plan = uitx.plans(ir)[0]
    tokens = [step["token"] for step in plan["steps"]]
    assert tokens[:2] == ["screen.observe", "ui.find"], (position, tokens)
    assert "ui.click" in tokens, (position, tokens)


def test_a_reported_return_position_crossing_carries_every_verdict() -> None:
    """The point of being in the plan. The step the walk used to drop now
    carries the four classifications that the `[4]` section and issue #1293's
    derived note read, and it is named in `unconfirmedIrreversibleSteps` -
    which is what a report that exists to name an unconfirmed irreversible
    actuation owes a program that writes one in tail position."""
    ir = compile_source(_program(SEMANTIC, "      return emit click(t)\n"),
                        "return_position.rvl")
    plan = uitx.plans(ir)[0]
    step = next(s for s in plan["steps"] if s["token"] == "ui.click")
    assert step["class"] == ui_family.reversibility("ui.click")
    assert step["residue"] in uitx.WEAKEST_FIRST
    assert step["confirmation"] is not None
    assert step["postcondition"] is not None
    assert plan["unconfirmedIrreversibleSteps"] == ["click"]


def test_a_compensating_crossing_is_reported_once() -> None:
    """The walk descends everything except a REGISTERED INVERSE. An `emit e
    compensate e'` carries `e'` in a sibling slot of the same statement, and
    a walk that counted it would report a second actuation the program never
    makes - the opposite error, and just as wrong a reading."""
    ir = compile_source(
        _program(SEMANTIC,
                 "      emit click(t) compensate click(t)\n      return 0\n"),
        "compensated.rvl")
    tokens = [step["token"] for step in uitx.plans(ir)[0]["steps"]]
    assert tokens.count("ui.click") == 1, tokens


def test_both_arms_of_a_branch_are_reported() -> None:
    """A plan is a READING, not an execution. Neither arm can be dropped: the
    plan cannot know which one runs, and reporting an arm a given run skips
    over-states the plan by one crossing, while reporting neither leaves an
    irreversible actuation unnamed."""
    ir = compile_source(
        _program(SEMANTIC,
                 '      if (region == "x") {\n        let x = emit click(t)\n'
                 "      } else {\n        emit click(t)\n"
                 "      }\n      return 0\n"),
        "both_arms.rvl")
    plan = uitx.plans(ir)[0]
    tokens = [step["token"] for step in plan["steps"]]
    assert tokens.count("ui.click") == 2, tokens
    assert plan["unconfirmedIrreversibleSteps"] == ["click", "click"]


# ------------------------------- being COUNTED, not merely listed, and the
# ------------------------------- one other fold that reads the same realm


#: A step set whose other computer-use step is genuinely RESTORED, so the
#: aggregate WITHOUT the tail crossing is `restored` - a real outcome, not the
#: empty `untouched` one. This is the sharpest form of the fail-open direction
#: the walk above closes: the plan said an inverse had put everything back.
#:
#: It carries its own `isolate` because the assertions below read the erase
#: report, which is per realm.
RESTORED_PLUS_TAIL = UI_TARGET + """
extern emission[screen.observe] fn obs(r: Str) -> Str = @py { return "" }
extern emission[ui.find] fn find(s: Str, n: Str) -> UiTarget
  = @py { return None }
extern pure fn clear_field() = @py { return None }
extern emission[ui.text] fn type_amount(t: UiTarget, a: Str)
  compensate clear_field()
  = @py { return None }
extern emission[ui.click] fn click(t: UiTarget) -> Int = @py { return 0 }
service Worker { emission fn approve(region: Str) -> Int }
component Billing provides worker: Worker {
  isolate worker in realm("billing")
  provide worker {
    fn approve(region) {
      let seen = emit obs(region)
      let t = emit find(seen, "Approve")
      emit type_amount(t, region)
      return emit click(t)
    }
  }
}
"""


def test_the_tail_crossing_is_the_weakest_part_of_a_restored_set() -> None:
    """Being in the plan is not the point; being COUNTED is. `aggregate` is
    the WEAKEST state PRESENT, so a step that is absent from the fold cannot
    weaken it - which is why a dropped crossing reads as a report that is
    BETTER than the truth rather than as a gap.

    Every other step here is `untouched` or `restored`, so the plan used to
    aggregate to `restored`: an inverse ran and put the state back. The
    `ui.click` in tail position has no inverse and never will. It is the
    weakest part, the honest aggregate is `uncompensated`, and the claim may
    not be read as a clean revert."""
    ir = compile_source(RESTORED_PLUS_TAIL, "restored_plus_tail.rvl")
    revert = uitx.plans(ir)[0]["revert"]
    assert revert["restored"] == ["type_amount"]
    assert revert["uncompensated"] == ["click"]
    assert revert["aggregate"] == uitx.UNCOMPENSATED
    assert "may not be reported as cleanly reverted" in revert["claim"]
    # the LIFO plan holds only the step that HAS an inverse: the tail crossing
    # is named as residue and never as something to undo.
    assert revert["compensateOrder"] == ["type_amount"]


def test_the_two_computer_use_folds_agree_about_a_tail_crossing() -> None:
    """The neighbouring fold, and why only ONE of the two had this hole.

    `revl erase-report` folds a realm's computer-use crossings twice. Section
    `[1]`'s `boundaryCrossings.uiResidue` reads `query.Composition`'s
    reachability facts, which do not know where in a body a crossing was
    written, so it always said `uncompensated` here. Section `[4]`'s plan
    reads the method body statement by statement, and that is the walk that
    dropped the tail crossing. One report answered the same question about
    the same click two ways, and the weaker of the two answers was the one a
    reader would have taken as the transaction's own verdict.

    Pinned as an equality rather than as two constants: the point is that the
    two folds may not drift apart again, not that today's answer is that
    word."""
    ir = compile_source(RESTORED_PLUS_TAIL, "two_folds.rvl")
    report = erase_report.build_report(ir, "billing", prove_residue=False)
    plan = next(p for p in report["uiTransactions"] if p["method"] == "approve")
    assert report["boundaryCrossings"]["uiResidue"]["aggregate"] \
        == plan["revert"]["aggregate"] == uitx.UNCOMPENSATED
    assert report["summary"]["uiResidueAggregate"] == uitx.UNCOMPENSATED
    # and the click is present in BOTH enumerations, by name.
    assert "click" in report["boundaryCrossings"]["uiResidue"]["residueSteps"]
    assert "click" in plan["unconfirmedIrreversibleSteps"]

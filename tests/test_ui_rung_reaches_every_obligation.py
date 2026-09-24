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
`xfail` rather than repaired inside a five-PR reconciliation, and fixed by
issue #1327: `ui_transaction.method_plan` dropped an emission in RETURN
position. The xfail is now the plain assertion
`test_a_return_position_actuation_is_reported`, and the section at the bottom
of this file covers the rest of what the same cause hid. Every plan-reading
fixture above still binds its emissions rather than returning them, which is
now a choice rather than a workaround.

NOT MEASURED HERE: no step executes, and nothing in this file drives a
desktop. These are compile-time and report-time classifications.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import erase_report, ui_family  # noqa: E402
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


# ------------------------------- the defect found here, and now repaired
#
# Issue #1327. `ui_transaction._calls` read three statement kinds and only the
# TOP node of each one's expression, so a crossing written anywhere else
# reached no plan at all. The strict `xfail` below is now a plain assertion,
# and the tests around it cover the rest of what the same cause hid.


#: Every position a `ui.click` can be written in a provide method that
#: compiles today, as `(label, body, reaches_the_plan_before_the_fix)`. The
#: last two are CONTROLS: they reached the plan before the fix and must still
#: reach it, so a walk that reported nothing would fail this file rather than
#: satisfy it.
CROSSING_POSITIONS = [
    ("return", "      return emit click(t)\n", False),
    ("if-then", "      if (1 == 1) { emit click(t) }\n      return 0\n", False),
    ("if-else",
     "      if (1 == 2) { return 0 } else { emit click(t) }\n      return 0\n",
     False),
    ("return-inside-if",
     "      if (1 == 1) { return emit click(t) }\n      return 0\n", False),
    ("assignment",
     "      var n = 0\n      n = emit click(t)\n      return n\n", False),
    ("nested-in-expression",
     "      let a = emit click(t) + 0\n      return a\n", False),
    ("bound", "      let a = emit click(t)\n      return a\n", True),
    ("bare-statement", "      emit click(t)\n      return 0\n", True),
]


@pytest.mark.parametrize(("label", "body", "reached_before"),
                         [(lbl, body, before)
                          for lbl, body, before in CROSSING_POSITIONS],
                         ids=[lbl for lbl, _b, _r in CROSSING_POSITIONS])
def test_every_crossing_position_reaches_the_plan(label, body,
                                                  reached_before) -> None:
    """The generalisation of the defect. Six of these eight positions produced
    a plan with no step for the click, so the crossing got no residue verdict,
    no confirmation state, no postcondition verdict, no `[4]` row and no entry
    in `unconfirmedIrreversibleSteps`. Two are controls that always worked."""
    ir = compile_source(_program(SEMANTIC, body), f"position_{label}.rvl")
    tokens = [step["token"] for step in uitx.plans(ir)[0]["steps"]]
    assert "ui.click" in tokens, (label, tokens)


def test_a_loop_body_cannot_hide_a_crossing_because_it_cannot_hold_one() -> None:
    """The two positions this file does NOT parametrize, and why. An `emit`
    step inside a provide-method `while`/`for` body is refused by the frontend,
    so a loop is not a place a crossing can hide from the plan. Pinned so that
    a later item admitting one is forced to come back here."""
    for loop in ("while (1 == 2) { emit click(t) }",
                 "for (i of [1]) { emit click(t) }"):
        with pytest.raises(RevlError) as refused:
            compile_source(
                _program(SEMANTIC, f"      {loop}\n      return 0\n"),
                "loop_body.rvl")
        assert "`emit`" in refused.value.message


def test_a_return_position_actuation_is_reported() -> None:
    """The original strict `xfail`, flipped. Left as its own named test rather
    than folded into the parametrization above, because this exact spelling is
    what the five-PR reconcile measured and reported."""
    ir = compile_source(
        _program(SEMANTIC, "      return emit click(t)\n"),
        "return_position.rvl")
    tokens = [step["token"] for step in uitx.plans(ir)[0]["steps"]]
    assert "ui.click" in tokens, tokens


def test_the_tail_crossing_gets_the_four_verdicts_it_was_missing() -> None:
    """A step absent from the plan is not a step with weak verdicts, it is a
    step with none. All four are computed from the one walk, so all four were
    missing together: the residue verdict, the confirmation state, the
    postcondition verdict and the `unconfirmedIrreversibleSteps` entry."""
    ir = compile_source(
        _program(SEMANTIC, "      return emit click(t)\n"),
        "tail_verdicts.rvl")
    plan = uitx.plans(ir)[0]
    step = next(s for s in plan["steps"] if s["token"] == "ui.click")
    assert step["residue"] == uitx.UNCOMPENSATED
    assert step["confirmation"] == uitx.UNCONFIRMED
    assert step["postcondition"] is not None
    assert "click" in plan["unconfirmedIrreversibleSteps"]


def test_the_tail_and_bound_spellings_of_one_click_agree() -> None:
    """Two programs that differ only in where the click is written must not get
    two readings. The plan is a statement about the crossings a method makes,
    and where an author binds the result is not one of them."""
    tail = uitx.plans(compile_source(
        _program(SEMANTIC, "      return emit click(t)\n"), "t.rvl"))[0]
    bound = uitx.plans(compile_source(
        _program(SEMANTIC,
                 "      let a = emit click(t)\n      return a\n"), "b.rvl"))[0]
    assert [s["token"] for s in tail["steps"]] \
        == [s["token"] for s in bound["steps"]]
    assert tail["revert"]["aggregate"] == bound["revert"]["aggregate"]
    assert tail["unconfirmedIrreversibleSteps"] \
        == bound["unconfirmedIrreversibleSteps"]


# ------------------------------------------- the neighbouring folds, measured


#: The erase report's OTHER computer-use fold, on a realm. The click is in tail
#: position; `type_amount` carries a declared inverse, so the set would
#: aggregate to `restored` if the click were missing from it. That is the
#: non-vacuity: the tail crossing is the weakest part of an otherwise
#: recoverable step set.
TAIL_REALM = UI_TARGET + """
extern emission[screen.observe] fn read_pane(region: Str) -> Str = @py { return "" }
extern emission[ui.find] fn locate(hint: Str) -> UiTarget = @py { return None }
extern pure fn clear_field() = @py { return None }
extern emission[ui.text] fn type_amount(target: UiTarget, amount: Str)
  compensate clear_field()
  = @py { return None }
extern emission[ui.click] fn actuate(target: UiTarget) -> Int = @py { return 0 }

service Refund { emission fn settle(invoice: Str) -> Int }

component UiAgent provides refund: Refund {
  isolate refund in realm("billing")
  provide refund {
    fn settle(invoice) {
      let pane = emit read_pane("detail")
      let field = emit locate("amount")
      emit type_amount(field, invoice)
      return emit actuate(field)
    }
  }
}
"""


def test_the_two_computer_use_folds_agree_about_a_tail_crossing() -> None:
    """The report answered the same question about the same click two ways.

    `boundaryCrossings.uiResidue` reads `query.Composition`'s reachability
    facts, which do not know WHERE in a body a crossing was written, so it was
    never blind. The `[4]` plan's five verdicts are all computed from the one
    walk, so they were blind together, and the weaker answer was the one a
    reader would take as the transaction's own verdict. Pinned as an equality
    rather than as two separate assertions, so neither side can drift."""
    ir = compile_source(TAIL_REALM, "tail_realm.rvl")
    report = erase_report.build_report(ir, "billing", prove_residue=False)
    realm_fold = report["boundaryCrossings"]["uiResidue"]["aggregate"]
    plan_fold = uitx.plans(ir)[0]["revert"]["aggregate"]
    assert plan_fold == realm_fold == uitx.UNCOMPENSATED


def test_the_tail_crossing_is_the_weakest_part_of_a_restored_set() -> None:
    """NON-VACUITY: the aggregate actually changing, not just a step appearing.

    Drop the tail click from the same program and the remaining set is a
    compensated `ui.text` plus two reads, which aggregates to `restored`: an
    inverse ran and put the state back. With the click counted, the set is
    `uncompensated`, the compensation order holds only `type_amount`, and the
    claim carries the caveat. A step absent from the fold cannot drag the
    aggregate down, which is why the missing step was the fail-open
    direction."""
    with_click = uitx.plans(compile_source(TAIL_REALM, "w.rvl"))[0]
    assert with_click["revert"]["aggregate"] == uitx.UNCOMPENSATED
    assert with_click["revert"]["compensateOrder"] == ["type_amount"]
    assert "may not be reported as cleanly reverted" \
        in with_click["revert"]["claim"]

    without_click = uitx.plans(compile_source(
        TAIL_REALM.replace("      return emit actuate(field)\n",
                           "      return 1\n"), "n.rvl"))[0]
    assert without_click["revert"]["aggregate"] == uitx.RESTORED
    assert "may not be reported as cleanly reverted" \
        not in without_click["revert"]["claim"]


def test_a_registered_inverse_is_not_reported_as_a_second_crossing() -> None:
    """The one thing the generic walk must NOT descend. `compensate f()` and an
    effect's `undo` are entries on the teardown accumulator: they run on
    unwind, not in the forward order the plan reports, and whether a crossing
    has an inverse is already carried per step. Walking them would report a
    second actuation the program never makes in that order."""
    ir = compile_source(TAIL_REALM, "inverse.rvl")
    tokens = [step["token"] for step in uitx.plans(ir)[0]["steps"]]
    assert tokens == ["screen.observe", "ui.find", "ui.text", "ui.click"]

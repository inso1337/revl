"""The transaction unit and the LIFO compensation run (roadmap item 522
slice 3, issue #1369, `docs/design/538-ui-transactions.md` §10).

WHAT WAS WRONG. `revert_report` produces `compensateOrder`: the reverse of the
step order, restricted to the steps that have an inverse. That is an ORDER and
not a RUN. It says nothing about where the transaction stopped, so on the
item's own five-step oracle it names the compensations of steps four and five,
which a failure at step three means never executed. Item 522's first exit
clause - "a mid-transaction failure runs the registered compensations in
order" - had nothing to be true of, because nothing in the artifact was keyed
on the failure.

`compensation_run` is keyed on it. `test_the_five_step_oracle` is 538 §10's
own sentence, and `test_the_order_a_plan_printed_before_this_names_steps_that_
never_ran` is the control that makes it a measurement rather than an addition.

WHAT RUNS. Nothing here drives a desktop. revl computes the run - its order,
its membership and its per-step outcome - and the compensating crossings are
performed by the computer-use substrate (roadmap item 539, upstream
`inso1337/revl-harness#11`), exactly as the actuations are. `performedBy`
carries that sentence in the artifact, and
`test_the_run_says_who_performs_it` asserts it is there, because a run that
did not say so would be read as an execution.

WHY THIS FILE DEPENDS ON SLICE 5. A LIFO run is started by an unmet
postcondition. So a run exists only for a step that HAS a postcondition, and
under the positional rule slice 5 replaced (issue #1370), every actuation
followed by any read anywhere had one - which would have produced a run for
steps whose failure the transaction can in fact never detect. The two slices
meet in `undetectableFailureSteps`, and `test_a_step_with_no_postcondition_
starts_no_run` is that meeting point.

NOT MEASURED HERE: no compensation is performed, so nothing in this file says
whether a performed compensation would succeed. A compensation that is
performed and FAILS has no word in item 522's five residue states, and this
module does not invent one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import erase_report  # noqa: E402
from revl import ui_transaction as uitx  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

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

#: Five actuating steps, in the classes the item's oracle needs: two
#: compensatable ones with declared inverses, then a `ui.click` (unknown, so
#: no inverse exists), then another compensatable one and a `ui.download`
#: (irreversible). The reads between them are what resolve each target and what
#: carries the third step's postcondition.
DECLARATIONS = """
extern emission[screen.observe] fn read_pane(region: Str) -> Str
  = @py { return "" }
extern emission[ui.find] fn locate(pane: Str, name: Str) -> UiTarget
  = @py { return None }
extern pure fn clear_amount() = @py { return None }
extern pure fn clear_memo() = @py { return None }
extern pure fn clear_note() = @py { return None }
extern emission[ui.text] fn type_amount(target: UiTarget, s: Str)
  compensate clear_amount()
  = @py { return None }
extern emission[ui.text] fn type_memo(target: UiTarget, s: Str)
  compensate clear_memo()
  = @py { return None }
extern emission[ui.click] fn actuate(target: UiTarget) = @py { return None }
extern emission[ui.text] fn type_note(target: UiTarget, s: Str)
  compensate clear_note()
  = @py { return None }
extern emission[ui.download] fn fetch_receipt(target: UiTarget) -> Str
  = @py { return "" }
"""

#: The item's five-step transaction. Step three is the `ui.click`, and it is
#: the one step whose postcondition is bound (slice 5): the read after it
#: resolves `Approve` again off a fresh observation. The other four have a read
#: somewhere after them and none of those reads checks them, so their failures
#: are the ones this transaction cannot detect.
FIVE_STEPS = UI_TARGET + DECLARATIONS + """
service Ops { emission fn run(region: Str) -> Int }
component Agent provides ops: Ops {
  isolate ops in realm("billing")
  provide ops {
    fn run(region) {
      let pane1 = emit read_pane(region)
      let amount = emit locate(pane1, "Amount")
      emit type_amount(amount, "10")
      let pane2 = emit read_pane(region)
      let memo = emit locate(pane2, "Memo")
      emit type_memo(memo, "m")
      let pane3 = emit read_pane(region)
      let approve = emit locate(pane3, "Approve")
      emit actuate(approve)
      let pane4 = emit read_pane(region)
      let checked = emit locate(pane4, "Approve")
      let pane5 = emit read_pane(region)
      let note = emit locate(pane5, "Note")
      emit type_note(note, "n")
      let pane6 = emit read_pane(region)
      let receipt = emit locate(pane6, "Attach")
      let saved = emit fetch_receipt(receipt)
      return 1
    }
  }
}
"""

#: The five computer-use steps of the transaction above, as
#: `compensation_run` takes them. Written out so the unit tests below do not
#: depend on the compiler, and asserted equal to what the plan reads off the
#: lowered program by `test_the_plan_and_the_unit_agree_on_the_steps`.
STEPS = [
    ("type_amount", "ui.text", True),
    ("type_memo", "ui.text", True),
    ("actuate", "ui.click", False),
    ("type_note", "ui.text", True),
    ("fetch_receipt", "ui.download", False),
]


def _plan() -> dict:
    return uitx.plans(compile_source(FIVE_STEPS, "run_1369.rvl"))[0]


def _actuations(plan: dict) -> list[str]:
    return [step["extern"] for step in plan["steps"]
            if step["postcondition"] != uitx.NOT_APPLICABLE]


# ------------------------------------------------------- the item's oracle


def test_the_five_step_oracle() -> None:
    """538 §10, slice 3, word for word: a five-step transaction whose third
    step fails runs exactly the compensations of steps two and one, in that
    order, and reports the third as `uncompensated`."""
    run = uitx.compensation_run(STEPS, "actuate")
    assert run["ran"] == ["type_memo", "type_amount"]
    outcomes = {entry["step"]: entry["outcome"] for entry in run["outcomes"]}
    assert outcomes["actuate"] == uitx.UNCOMPENSATED
    assert run["failedStep"] == "actuate"
    assert run["failedStepIndex"] == 2


def test_a_step_after_the_failure_never_executed_and_is_untouched() -> None:
    """`untouched` is the right word and it is not a euphemism: the
    transaction stopped before those steps, so they changed no state the target
    owns. Calling them `restored` would claim an inverse ran; calling them
    residue would ask an auditor to handle something that never happened."""
    run = uitx.compensation_run(STEPS, "actuate")
    assert run["neverExecuted"] == ["type_note", "fetch_receipt"]
    later = [entry for entry in run["outcomes"]
             if entry["step"] in ("type_note", "fetch_receipt")]
    assert [entry["outcome"] for entry in later] == [uitx.UNTOUCHED] * 2
    assert all(entry["executed"] is False for entry in later)


def test_the_order_a_plan_printed_before_this_names_steps_that_never_ran(
) -> None:
    """THE CONTROL, and the reason the oracle above is a measurement. The
    artifact item 522 had before this slice is `compensateOrder`, which is not
    keyed on the failure: it names `type_note`, a step the failure at `actuate`
    means never executed. Three labels against the run's two."""
    order = uitx.revert_report(STEPS)["compensateOrder"]
    assert order == ["type_note", "type_memo", "type_amount"]
    run = uitx.compensation_run(STEPS, "actuate")
    assert "type_note" in order and "type_note" not in run["ran"]
    assert len(order) == 3 and len(run["ran"]) == 2


def test_the_run_is_lifo_and_not_merely_a_set() -> None:
    """G7's own word. Reversing the two compensations would still be "the
    compensations of steps two and one"; the order is the claim."""
    assert uitx.compensation_run(STEPS, 4)["ran"] == [
        "type_note", "type_memo", "type_amount"]
    assert uitx.compensation_run(STEPS, 1)["ran"] == [
        "type_memo", "type_amount"]
    assert uitx.compensation_run(STEPS, 0)["ran"] == ["type_amount"]


def test_the_failing_steps_own_compensation_runs_when_it_has_one() -> None:
    """The rule the oracle does not pin, because its third step has no
    inverse. An unmet postcondition says revl could not SEE the effect land,
    which is not knowing it did not, so a registered inverse is run: restoring
    a field is correct whether or not the typing arrived, and skipping it would
    leave residue on the other reading."""
    run = uitx.compensation_run(STEPS, "type_memo")
    assert run["ran"][0] == "type_memo"


def test_a_step_with_no_inverse_stays_uncompensated_after_the_run() -> None:
    """Item 522's second exit clause. No run of any compensation set changes
    the fact that no inverse exists, and the artifact must not print a word
    that suggests otherwise."""
    run = uitx.compensation_run(STEPS, 4)
    outcomes = {entry["step"]: entry["outcome"] for entry in run["outcomes"]}
    assert outcomes["actuate"] == uitx.UNCOMPENSATED
    assert outcomes["fetch_receipt"] == uitx.UNCOMPENSATED
    assert run["aggregate"] == uitx.UNCOMPENSATED
    assert "cleanly reverted" in run["claim"]
    assert run["claim"].startswith("the transaction stopped at")
    reasons = {entry["step"]: entry["reason"] for entry in run["notRun"]}
    assert "no inverse exists" in reasons["fetch_receipt"]
    assert "cannot tell" in reasons["actuate"]


def test_a_failure_this_module_cannot_locate_is_refused() -> None:
    """The fail-closed direction. A run over a failure with no step would be a
    run over the whole transaction, which is the answer that compensates the
    most and proves the least."""
    with pytest.raises(LookupError):
        uitx.compensation_run(STEPS, "no_such_step")
    with pytest.raises(LookupError):
        uitx.compensation_run(STEPS, 5)


def test_the_run_says_who_performs_it() -> None:
    """revl computes the run. The crossings are the substrate's, and the
    artifact says so where it is read rather than only in a design doc."""
    run = uitx.compensation_run(STEPS, 0)
    assert "item 539" in run["performedBy"]
    assert "Nothing in revl drives a desktop." in run["performedBy"]


# ------------------------------------------------- the run inside the plan


def test_the_plan_and_the_unit_agree_on_the_steps() -> None:
    plan = _plan()
    assert _actuations(plan) == [label for label, _t, _c in STEPS]


def test_the_plan_carries_the_run_for_the_step_it_can_detect() -> None:
    """One run, for the one step whose postcondition is bound. Slice 5 is what
    makes that a short list rather than every actuation in the method."""
    plan = _plan()
    assert len(plan["compensationRuns"]) == 1
    run = plan["compensationRuns"][0]
    assert run["failedStep"] == "actuate"
    assert run["ran"] == ["type_memo", "type_amount"]


def test_a_step_with_no_postcondition_starts_no_run() -> None:
    """WHERE SLICE 3 MEETS SLICE 5, and the honest limit of both. A LIFO run is
    started by an unmet postcondition. Four of these five steps have no
    postcondition bound to them, so a failure at any of them is a failure the
    transaction never learns about and no compensation runs at all. That is
    named rather than papered over: the alternative is a plan carrying four
    runs that nothing would ever trigger."""
    plan = _plan()
    assert plan["undetectableFailureSteps"] == [
        "type_amount", "type_memo", "type_note", "fetch_receipt"]
    started = {run["failedStep"] for run in plan["compensationRuns"]}
    assert started.isdisjoint(set(plan["undetectableFailureSteps"]))


def test_the_report_renders_the_run_and_calls_it_a_run() -> None:
    report = erase_report.build_report(
        compile_source(FIVE_STEPS, "run_1369.rvl"), "billing",
        prove_residue=False)
    rendered = erase_report.render(report)
    assert "LIFO COMPENSATION RUN" in rendered
    assert ("if actuate() fails: type_memo(), type_amount()") in rendered
    assert "never executed, so nothing to undo: type_note(), fetch_receipt()" \
        in rendered
    assert "residue after the run: actuate() uncompensated" in rendered
    assert "the substrate performs the crossings" in rendered


def test_the_report_still_refuses_to_call_the_result_clean() -> None:
    """The word this whole item exists to keep out of the artifact. A run that
    compensated two of three executed steps is not a clean teardown, and the
    report may not summarise it as one."""
    report = erase_report.build_report(
        compile_source(FIVE_STEPS, "run_1369.rvl"), "billing",
        prove_residue=False)
    rendered = erase_report.render(report)
    assert "rolled back" not in rendered
    assert "no_residue" not in rendered


# ---------------------------------------- the unit over a tail-position step
#
# WHY THIS SECTION EXISTS. Slice 3's oracle above compiles a transaction whose
# every crossing is BOUND (`emit f(...)` as a statement, or `let x = emit
# f(...)`). That was not a stylistic choice: `method_plan`'s walk read three
# statement shapes and only the top node of each one's expression, so an
# actuation written anywhere else was not a step in the plan at all, and the
# transaction unit was therefore a unit over a step set with a hole in it.
#
# Tail position is the case that matters here, because a `provide` method that
# returns what it clicked has nothing left to bind, so it is where an actuation
# most naturally lands. The run below is the item's first exit clause applied
# to exactly that shape, and it is a measurement rather than an addition: on
# the walk this replaces, `compensation_run` keyed on the tail step raised
# `LookupError: no computer-use step named 'actuate' in this transaction:
# ['type_amount', 'type_memo']`. The transaction could not be told which step
# failed, because the step it failed at was not in the transaction.
#
# The fail-open direction is the same one issue #1327 names. A run keyed by
# INDEX rather than by label did not raise at all on the old walk: it ran over
# the two compensatable steps, every one of them restored, and reported an
# aggregate of `restored` for a transaction holding an uncompensated click.
#
# WHICH OF THE FOUR ARE MEASUREMENTS, stated so the section is not read as
# four. Two of them read the PLAN and fail on the walk this replaces:
# `test_the_transaction_unit_sees_a_tail_position_actuation` and
# `test_a_tail_position_failure_the_transaction_cannot_detect_is_named`. The
# other two call `compensation_run` directly with the step list written out
# above, so they pass on both walks by construction. They are the controls
# that say what the run over the complete step set is entitled to claim, and
# what the run over the incomplete one claimed instead.

#: The five-step oracle's opening three steps, with the `ui.click` moved into
#: return position and nothing else changed.
TAIL_TRANSACTION = UI_TARGET + DECLARATIONS + """
service Ops { emission fn run(region: Str) -> Int }
component Agent provides ops: Ops {
  isolate ops in realm("billing")
  provide ops {
    fn run(region) {
      let pane1 = emit read_pane(region)
      let amount = emit locate(pane1, "Amount")
      emit type_amount(amount, "10")
      let pane2 = emit read_pane(region)
      let memo = emit locate(pane2, "Memo")
      emit type_memo(memo, "m")
      let pane3 = emit read_pane(region)
      let approve = emit locate(pane3, "Approve")
      return emit actuate(approve)
    }
  }
}
"""

#: What that transaction's three actuating steps are, in `compensation_run`'s
#: shape. Asserted equal to what the plan reads off the lowered program by
#: `test_the_transaction_unit_sees_a_tail_position_actuation`, which is the
#: assertion that fails on the walk this replaces.
TAIL_STEPS = [
    ("type_amount", "ui.text", True),
    ("type_memo", "ui.text", True),
    ("actuate", "ui.click", False),
]


def _tail_plan() -> dict:
    return uitx.plans(compile_source(TAIL_TRANSACTION, "tail_1369.rvl"))[0]


def test_the_transaction_unit_sees_a_tail_position_actuation() -> None:
    """The step set the run is computed over holds the tail crossing. Before
    the walk that reads every position, the plan named `type_amount` and
    `type_memo` and stopped, so the click was not a step of the transaction it
    is a step of."""
    plan = _tail_plan()
    assert _actuations(plan) == [label for label, _t, _c in TAIL_STEPS]


def test_the_run_over_a_tail_position_failure_is_the_exit_clause() -> None:
    """Item 522's first exit clause, on the shape that used to have no step:
    the failure is at the tail actuation, the two compensations before it run
    in reverse, and the failing step itself reports `uncompensated` rather than
    a clean teardown."""
    run = uitx.compensation_run(TAIL_STEPS, "actuate")
    assert run["failedStep"] == "actuate"
    assert run["ran"] == ["type_memo", "type_amount"]
    assert run["neverExecuted"] == []
    outcomes = {entry["step"]: entry["outcome"] for entry in run["outcomes"]}
    assert outcomes == {
        "type_amount": uitx.RESTORED,
        "type_memo": uitx.RESTORED,
        "actuate": uitx.UNCOMPENSATED,
    }
    assert run["aggregate"] == uitx.UNCOMPENSATED


def test_the_tail_step_is_what_drags_that_run_down() -> None:
    """The non-vacuity, and the reason a dropped step read BETTER than the
    truth. `aggregate` is the weakest state present, so the run over the two
    steps the old walk could see reports `restored`: an inverse ran and put the
    state back. The same transaction with its actual third step reports
    `uncompensated`, and the claim gains the sentence that matters."""
    without = uitx.compensation_run(TAIL_STEPS[:2], "type_memo")
    assert without["aggregate"] == uitx.RESTORED
    with_tail = uitx.compensation_run(TAIL_STEPS, "actuate")
    assert with_tail["aggregate"] == uitx.UNCOMPENSATED
    # and the sentence the claim gains with it. The run the old walk could see
    # stops after counting the compensations; the real one has to say that
    # residue no inverse describes remains, and refuse the clean word.
    assert without["claim"] == (
        "the transaction stopped at `type_memo`; 2 registered compensations "
        "run, LIFO")
    assert with_tail["claim"] == (
        "the transaction stopped at `actuate`; 2 registered compensations "
        "run, LIFO. Residue no inverse describes remains at `actuate`. This "
        "transaction may not be reported as cleanly reverted")


def test_a_tail_position_failure_the_transaction_cannot_detect_is_named() -> \
        None:
    """WHERE THIS SLICE STOPS, stated as an assertion rather than left to the
    reader. A LIFO run is started by an unmet postcondition, and a tail-
    position actuation has nothing after it that could check it, so its failure
    is one this transaction never learns about. The step is now IN the plan and
    carries every verdict, and it is named in `undetectableFailureSteps`
    instead of being given a run nothing would trigger. Being invisible to the
    plan and being named as undetectable are different answers, and only the
    second one is honest."""
    plan = _tail_plan()
    assert "actuate" in plan["undetectableFailureSteps"]
    assert plan["compensationRuns"] == []
    actuate = [s for s in plan["steps"] if s["extern"] == "actuate"]
    assert len(actuate) == 1
    assert actuate[0]["residue"] == uitx.UNCOMPENSATED
    assert actuate[0]["postcondition"] in uitx.NO_POSTCONDITION

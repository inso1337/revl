"""The postcondition is bound to its STEP, not to its position (roadmap item
522 slice 5, issue #1370, `docs/design/538-ui-transactions.md` §10).

WHAT WAS WRONG. `ui_transaction.method_plan` decided a postcondition
positionally: any later reversible crossing anywhere in the same method made
every earlier actuation `verified-against-untrusted-read`. That is the sentence
"a read follows this actuation", and the report printed it as though it were
"a read checks this actuation". The two differ on a program that is easy to
write by accident and easy to write on purpose: click Approve, then read
Cancel. PR #1287's own "what is not verified" named this, and it is the reason
slice 5 lands beside slice 3 rather than after it - a LIFO compensation run
started by an unmet postcondition, built on a postcondition that is really an
index, would report a guarantee it does not have.

WHAT BINDS A READ TO A STEP HERE, and what it is worth. Item 521 slice 4
carries a `UiTarget` BY VALUE; it is not a resolved handle
(`docs/design/565-ui-target-binding.md` §7 and §11). So the binding is over how
the two crossings NAME the target: a later `ui.find` carries this step's
postcondition when it resolves the target by the same provenance and reads it
from an observation taken AFTER the actuation. That is re-resolution by name.
It is strictly weaker than a handle and it does NOT close the check-to-use race
(issue #1371): two resolutions of one name at two instants may return two
different controls. It is nonetheless a different statement from position, and
this file measures the difference.

THE LIMIT THAT IS NOT ENGINEERED AWAY (538 §4). The read is itself
attacker-influenced content: `screen.observe` is an untrusted source under item
521 slice 2, and `ui.find` derives from it. So the strongest word any of this
reaches is `verified-against-untrusted-read`, and
`test_the_bound_read_is_still_an_untrusted_read` asserts that binding the read
did not promote it.

NOT MEASURED HERE: nothing executes. No step crosses, no desktop is driven,
and no postcondition is evaluated against a real screen. These are compile-time
readings of a lowered composition.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import erase_report, ui_family  # noqa: E402
from revl import ui_transaction as uitx  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

#: Item 521 slice 4's record. Nothing here measures it; the programs below
#: would not compile without it.
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

DECLARATIONS = """
extern emission[screen.observe] fn read_pane(region: Str) -> Str
  = @py { return "" }
extern emission[ui.find] fn locate(pane: Str, name: Str) -> UiTarget
  = @py { return None }
extern emission[ui.click] fn actuate(target: UiTarget) = @py { return None }
extern emission[ui.click] fn actuate_pair(a: UiTarget, b: UiTarget)
  = @py { return None }
"""


def _program(body: str) -> str:
    return (UI_TARGET + DECLARATIONS
            + "service Ops { emission fn run(region: Str) -> Int }\n"
              "component Agent provides ops: Ops {\n"
              "  isolate ops in realm(\"billing\")\n"
              "  provide ops {\n"
              "    fn run(region) {\n"
            + body
            + "      return 1\n"
              "    }\n  }\n}\n")


#: Click `Approve`, then read `Approve` again off a FRESH observation. The
#: control, and the shape design 538 §3.1 asks for.
CHECKS_THE_ACTUATED_TARGET = _program(
    '      let pane = emit read_pane(region)\n'
    '      let target = emit locate(pane, "Approve")\n'
    '      emit actuate(target)\n'
    '      let fresh = emit read_pane(region)\n'
    '      let seen = emit locate(fresh, "Approve")\n')

#: Click `Approve`, then read `Cancel`. THE EXIT PROGRAM. Positionally it is
#: indistinguishable from the control: a reversible crossing follows the
#: actuation in the same method, which is all the old rule asked.
CHECKS_ANOTHER_TARGET = _program(
    '      let pane = emit read_pane(region)\n'
    '      let target = emit locate(pane, "Approve")\n'
    '      emit actuate(target)\n'
    '      let fresh = emit read_pane(region)\n'
    '      let seen = emit locate(fresh, "Cancel")\n')

#: Click `Approve`, then read `Approve` off the observation taken BEFORE the
#: click. The right target, the wrong instant: this reports the screen as it
#: was before the step ran, which is not a postcondition at all.
CHECKS_A_STALE_OBSERVATION = _program(
    '      let pane = emit read_pane(region)\n'
    '      let target = emit locate(pane, "Approve")\n'
    '      emit actuate(target)\n'
    '      let seen = emit locate(pane, "Approve")\n')

#: No read after the actuation at all. `unverified` is the true word here and
#: stays it: slice 5 must not turn every unverified step into its new one.
NO_READ_FOLLOWS = _program(
    '      let pane = emit read_pane(region)\n'
    '      let target = emit locate(pane, "Approve")\n'
    '      emit actuate(target)\n')

#: An actuation whose extern declares TWO `UiTarget` parameters. Item 521 slice
#: 4 admits it - the record check is a floor - and `565 §7` says plainly that a
#: capability token carries no parameter roles, so which parameter is the
#: target is undecided. Fail closed: no binding, and the read that follows is
#: reported as not bound rather than credited to a guess.
TWO_TARGET_PARAMETERS = _program(
    '      let pane = emit read_pane(region)\n'
    '      let one = emit locate(pane, "Approve")\n'
    '      let two = emit locate(pane, "Cancel")\n'
    '      emit actuate_pair(one, two)\n'
    '      let fresh = emit read_pane(region)\n'
    '      let seen = emit locate(fresh, "Approve")\n')


def _plan(source: str) -> dict:
    return uitx.plans(compile_source(source, "postcondition_1370.rvl"))[0]


def _step(plan: dict, extern: str) -> dict:
    for step in plan["steps"]:
        if step["extern"] == extern:
            return step
    raise AssertionError(f"no step for extern {extern!r}")


def _a_read_follows_the_actuation(plan: dict, extern: str) -> bool:
    """THE OLD RULE, computed here so the control is in the same file as the
    claim. This is exactly what `method_plan` used to decide the verdict with:
    a later crossing of a reversible verb, anywhere in the method."""
    at = [i for i, step in enumerate(plan["steps"])
          if step["extern"] == extern][0]
    return any(ui_family.reversibility(step["token"]) == ui_family.REVERSIBLE
               for step in plan["steps"][at + 1:])


# ------------------------------------------------------------ the vocabulary


def test_the_four_postcondition_words_are_the_documented_ones() -> None:
    """`POSTCONDITION_MEANING` is what the report prints, so a word with no
    meaning entry is a word an auditor reads and cannot look up."""
    assert set(uitx.POSTCONDITION_MEANING) == set(uitx.POSTCONDITION_STATES)
    assert len(uitx.POSTCONDITION_STATES) == 4
    assert uitx.UNBOUND not in (uitx.UNVERIFIED,
                                uitx.VERIFIED_AGAINST_UNTRUSTED,
                                uitx.NOT_APPLICABLE)


def test_the_new_word_exists_because_one_word_covered_two_outcomes() -> None:
    """The same argument the five residue states are built on, applied to the
    postcondition vocabulary: `unverified` says no read follows, which is false
    for a program that reads something else, and the verified word would credit
    the step with a check of another control."""
    assert uitx.postcondition("ui.click", checked_by_read=False,
                              followed_by_read=False) == uitx.UNVERIFIED
    assert uitx.postcondition("ui.click", checked_by_read=False,
                              followed_by_read=True) == uitx.UNBOUND
    assert uitx.UNBOUND in uitx.NO_POSTCONDITION
    assert uitx.UNVERIFIED in uitx.NO_POSTCONDITION
    assert uitx.VERIFIED_AGAINST_UNTRUSTED not in uitx.NO_POSTCONDITION


# ------------------------------------------------------------- the exit test


def test_a_read_of_another_target_does_not_verify_the_actuation() -> None:
    """THE EXIT (issue #1370). The program clicks `Approve` and reads
    `Cancel`. It satisfies the positional rule - a read does follow - and it
    must not satisfy the postcondition."""
    plan = _plan(CHECKS_ANOTHER_TARGET)
    step = _step(plan, "actuate")
    assert _a_read_follows_the_actuation(plan, "actuate") is True
    assert step["postcondition"] == uitx.UNBOUND
    assert step["postconditionCheckedBy"] is None
    assert plan["unboundPostconditionSteps"] == ["actuate"]
    assert "actuate" in plan["undetectableFailureSteps"]


def test_the_control_reads_the_target_it_actuated_and_passes() -> None:
    """The non-vacuity of the assertion above: the two programs differ in one
    string literal, and only one of them reaches the verified word. The read
    is NAMED, so an auditor can go and look at it."""
    plan = _plan(CHECKS_THE_ACTUATED_TARGET)
    step = _step(plan, "actuate")
    assert step["postcondition"] == uitx.VERIFIED_AGAINST_UNTRUSTED
    assert step["postconditionCheckedBy"] == "locate"
    assert plan["unboundPostconditionSteps"] == []
    assert plan["undetectableFailureSteps"] == []


def test_the_two_programs_differ_by_one_literal() -> None:
    """Stated as a measurement rather than as a claim about the fixtures: the
    control and the exit program are the same source but for the name the
    post-actuation read resolves."""
    assert CHECKS_ANOTHER_TARGET == CHECKS_THE_ACTUATED_TARGET.replace(
        'locate(fresh, "Approve")', 'locate(fresh, "Cancel")')


# ----------------------------------------------------------- the other halves


def test_a_read_off_a_pre_actuation_observation_is_not_a_postcondition(
) -> None:
    """The freshness half. The read resolves the right target from the
    observation taken BEFORE the click, so it reports the screen as it was
    before the step ran. 538 §3.1 asks for a FRESH `screen.observe` / `ui.find`
    pair and this is the case that word is doing work in."""
    plan = _plan(CHECKS_A_STALE_OBSERVATION)
    step = _step(plan, "actuate")
    assert _a_read_follows_the_actuation(plan, "actuate") is True
    assert step["postcondition"] == uitx.UNBOUND


def test_an_actuation_with_no_read_after_it_is_still_unverified() -> None:
    """Slice 5 must not collapse the two words in the other direction: a step
    with no read following it is `unverified`, and saying a read checked
    something else would be false."""
    plan = _plan(NO_READ_FOLLOWS)
    step = _step(plan, "actuate")
    assert _a_read_follows_the_actuation(plan, "actuate") is False
    assert step["postcondition"] == uitx.UNVERIFIED
    assert plan["unverifiedSteps"] == ["actuate"]
    assert plan["unboundPostconditionSteps"] == []


def test_two_target_parameters_bind_nothing_and_say_so() -> None:
    """`docs/design/565-ui-target-binding.md` §7: a capability token carries no
    parameter roles, so the signature check says a parameter is a `UiTarget`
    and not WHICH one. With two, this module declines to pick, and the step is
    reported as unbound rather than credited against a guessed target. That is
    the fail-closed side, and it is issue #1371's second half showing up as a
    report line rather than as a refusal."""
    plan = _plan(TWO_TARGET_PARAMETERS)
    step = _step(plan, "actuate_pair")
    assert step["targetParameters"] == 2
    assert step["postcondition"] == uitx.UNBOUND
    assert step["postconditionCheckedBy"] is None


def test_the_step_names_the_find_that_resolved_its_target() -> None:
    """Reported, not acted on. `targetResolvedBy` is the `ui.find` in this
    method whose result the step acted on, and it is what issue #1371's minimum
    exit would need; naming it here does not close that race, because nothing
    revalidates the binding at the moment of use."""
    plan = _plan(CHECKS_THE_ACTUATED_TARGET)
    assert _step(plan, "actuate")["targetResolvedBy"] == "locate"


def test_the_bound_read_is_still_an_untrusted_read() -> None:
    """538 §4, kept in the report rather than engineered away. Binding the read
    to the step says WHICH control it looked at. It does not make the
    application's own answer about that control trustworthy: `ui.find` is a
    source under item 521 slice 2, so there is no plain `verified` here and
    the printed meaning says why."""
    plan = _plan(CHECKS_THE_ACTUATED_TARGET)
    assert _step(plan, "actuate")["postcondition"] \
        == "verified-against-untrusted-read"
    assert "verified" not in set(uitx.POSTCONDITION_MEANING)
    assert ui_family.SOURCE in ui_family.taint_roles("ui.find")
    assert "RAISES CONFIDENCE" in uitx.POSTCONDITION_MEANING[
        uitx.VERIFIED_AGAINST_UNTRUSTED]


# ------------------------------------------------------------- the report


def test_the_report_names_the_read_that_carries_the_postcondition() -> None:
    report = erase_report.build_report(
        compile_source(CHECKS_THE_ACTUATED_TARGET, "postcondition_1370.rvl"),
        "billing", prove_residue=False)
    rendered = erase_report.render(report)
    assert "postcondition read: locate()" in rendered


def test_the_report_names_the_step_whose_read_checks_something_else(
) -> None:
    report = erase_report.build_report(
        compile_source(CHECKS_ANOTHER_TARGET, "postcondition_1370.rvl"),
        "billing", prove_residue=False)
    rendered = erase_report.render(report)
    assert "READ NOT BOUND TO THE STEP: actuate" in rendered
    assert "NO FAILURE DETECTION: actuate" in rendered

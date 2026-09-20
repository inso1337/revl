"""UI transaction phases, the revert split and the confirmation gate (roadmap
item 522, issue #1196, Slice 2 of docs/design/553-ui-transaction-phases.md).

Slice 1 (`tests/test_ui_transaction_classification_522.py`) landed the half
that stops a program CLAIMING cleanliness: a registry-owned reversibility class
per computer-use verb, and two refusals over the `compensate` slot. This file
covers the half that reports what actually happened, and the gate over it.

THE MEASURED WORD. Before this change `revl erase-report` tagged
`screen.observe` and `ui.click` with the SAME word, `bare`, under a note saying
a bare crossing "left the system with nothing done about it ... so it can be
handled out of band". `screen.observe` is a read: nothing needs handling.
`ui.click` has no inverse: nothing CAN be done. One word, three readings, two
of them false. `test_a_read_is_not_reported_as_residue` and
`test_a_click_and_a_read_no_longer_share_a_word` are that measurement.

THE GATE. `capability ui.click requires approval` is the operator's authority
raising a verb to `confirm-required`. `policy.approval_admission` has always
enforced it — and was CALLED only from `revl.mcp.session`, so the surface an
operator reads before shipping, `revl audit --policy`, reported the very same
composition CLEAN and exited 0 while a session refused it. That is the
confirmation gate's missing half and it is what
`test_audit_policy_refuses_an_unconfirmed_ui_crossing` closes.

FAILURE DIRECTION, for every assertion here:

  * the revert aggregate is the WEAKEST part, never an average, so one
    uncompensated click cannot be diluted by four clean steps (item 546 rule 3,
    PR #1256);
  * `unregistered` is ordered BELOW `uncompensated`, because an omission must
    not inherit the exit of a stated class — item 546's `undeclared`-below-
    `neither` argument, and this item's own design note calling a missing
    inverse "the worst of the three outcomes: not restored, not reported";
  * a token this module does not recognise gets NO residue verdict rather than
    a permissive one, so the change cannot silently re-label a crossing it does
    not understand;
  * a postcondition is never reported as plain `verified`. The strongest word
    in the vocabulary is `verified-against-untrusted-read`, and
    `test_there_is_no_plain_verified_verdict` asserts the absence so a later
    slice cannot quietly add one.

NON-VACUITY. Measured against this branch's base, origin/main at
`dfecba2a`. The numbers are in the PR body and in the design note; the
controls are `test_a_composition_without_a_ui_verb_is_untouched` (the report
is byte-identical there) and
`test_a_policy_that_raises_nothing_is_still_clean` (the gate does not fire
without a rule) and `test_an_activation_body_crossing_with_an_edge_admits`
(the gate is not a blanket refusal).

WHAT IS NOT ASSERTED HERE, because it is not built: no phase executes, no
compensation runs, nothing is scheduled. The phases are a compile-time
ELIGIBILITY decision and a static plan read off the lowered IR. The substrate
that would execute them is roadmap item 539, upstream
`inso1337/revl-harness#11`.
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
from revl.policy import approval_admission, parse_policy  # noqa: E402

# --------------------------------------------------------------- programs
#
# Inline, as Python strings. `.rvl` files under a corpus root enter the
# reference census: the item-525 lane measured exactly one `false-admit/G4` and
# one `false-reject/BAD` from dropping computer-use programs in as files, and
# `tests/test_model_placement_512.py` keeps its programs inline for the same
# reason.

#: A computer-use agent whose loop lives in a `provide` method — the shape the
#: item is about, and the shape that cannot acquire an `Approval[C]`.
AGENT = """
service Ledger { emission[db.invoice] fn adjust(invoice: Str) -> Str }

extern emission[screen.observe] fn read_pane(region: Str) -> Str = @py { return "" }
extern emission[ui.find] fn locate(hint: Str) -> Str = @py { return "" }
extern pure fn clear_field() = @py { return None }
extern emission[ui.text] fn type_amount(target: Str, amount: Str)
  compensate clear_field()
  = @py { return None }
extern emission[ui.click] fn actuate(target: Str) = @py { return None }
extern emission[ui.download] fn fetch_receipt(target: Str) -> Str = @py { return "" }

service Refund { emission fn settle(invoice: Str) -> Str }

component UiAgent requires ledger: Ledger provides refund: Refund {
  isolate refund in realm("billing")
  provide refund {
    fn settle(invoice) {
      let pane = emit read_pane("detail")
      let field = emit locate("amount")
      emit type_amount("amount", invoice)
      emit actuate("Apply")
      let receipt = emit fetch_receipt("receipt.pdf")
      let posted = emit ledger.adjust(invoice)
      return posted
    }
  }
}
"""

#: The same reach with NO computer-use verb. The control: every assertion this
#: file makes about the report must leave this one alone.
NO_UI = """
service Ledger { emission[db.invoice] fn adjust(invoice: Str) -> Str }
extern emission[net.post] fn publish(body: Str) = @py { return None }
service Refund { emission fn settle(invoice: Str) -> Str }

component PlainAgent requires ledger: Ledger provides refund: Refund {
  isolate refund in realm("billing")
  provide refund {
    fn settle(invoice) {
      emit publish(invoice)
      let posted = emit ledger.adjust(invoice)
      return posted
    }
  }
}
"""

#: A click in the ACTIVATION body, with a covering `Approval[ui.click]` edge.
#: The only shape in which a computer-use crossing is confirmable today.
CONFIRMED = """
extern emission[ui.click] fn actuate(target: Str) = @py { return None }
service Ops { fn ping() -> Int }
component Clicker provides ops: Ops {
  let a = await approval["ui.click"] { target: 1 }
  emit actuate("Apply") with a
  provide ops { fn ping() = 1 }
}
"""

#: The same activation-body click with NO edge.
UNCONFIRMED = """
extern emission[ui.click] fn actuate(target: Str) = @py { return None }
service Ops { fn ping() -> Int }
component Clicker provides ops: Ops {
  emit actuate("Apply")
  provide ops { fn ping() = 1 }
}
"""


def _report(source: str, realm: str = "billing") -> dict:
    return erase_report.build_report(
        compile_source(source, "uitx522.rvl"), realm, prove_residue=False)


def _crossing(report: dict, name: str) -> dict:
    for entry in report["boundaryCrossings"]["externs"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"no crossing named {name!r}")


# ------------------------------------------------------- the phase decision


def test_the_eight_phases_are_the_ones_the_item_proposes() -> None:
    """observe, plan, stage, preview, confirm, commit, verify, compensate — in
    the item's own order, and each with a stated owner."""
    assert uitx.PHASES == ("observe", "plan", "stage", "preview", "confirm",
                           "commit", "verify", "compensate")
    assert set(uitx.OWNER) == set(uitx.PHASES)
    assert set(uitx.DECIDES) == set(uitx.PHASES)


def test_a_phase_revl_decides_nothing_about_says_so() -> None:
    """`plan`, `stage` and `preview` map to `None`. An honest nothing is what
    keeps the table from being a promise revl does not keep: an implied
    guarantee nothing enforces is worse than no phase list at all."""
    assert uitx.DECIDES[uitx.PLAN] is None
    assert uitx.DECIDES[uitx.STAGE] is None
    assert uitx.DECIDES[uitx.PREVIEW] is None
    assert uitx.DECIDES[uitx.COMPENSATE] is not None


def test_a_step_with_no_inverse_may_not_occupy_the_compensate_phase() -> None:
    """The item in one table cell. A transaction may not place a step with no
    inverse in the phase whose name means it was undone."""
    assert not uitx.may_compensate("ui.click")
    assert not uitx.may_compensate("ui.download")
    assert uitx.may_compensate("ui.text")


def test_a_read_is_eligible_for_verify_and_not_for_commit() -> None:
    """Design 538 §3.1: a `verify` phase built from a fresh `screen.observe` /
    `ui.find` pair does not grow the residue it is checking."""
    for token in ("screen.observe", "ui.find"):
        phases = uitx.eligible_phases(token)
        assert uitx.VERIFY in phases
        assert uitx.COMMIT not in phases
        assert uitx.COMPENSATE not in phases


def test_every_admissible_verb_has_a_phase_row() -> None:
    """Closed and total over the registry, like the class table it reads. A
    verb added without a phase row fires here rather than shipping with an
    empty eligibility, which would read as `eligible for nothing` and be
    indistinguishable from `not a computer-use verb`."""
    for token in ui_family.spellings():
        assert uitx.eligible_phases(token), token


def test_a_non_ui_token_gets_no_phase_and_no_verdict() -> None:
    """The failure direction on an unrecognised token: this module declines to
    speak rather than defaulting to a permissive answer."""
    assert uitx.eligible_phases("db.write") == ()
    assert uitx.residue_state("db.write", False) is None
    assert uitx.confirmation("db.write", covered=False, raised=True) is None
    assert uitx.postcondition("db.write", followed_by_read=True) is None


# ---------------------------------------------------------- the revert split


def test_the_five_residue_states_are_disjoint_and_ordered() -> None:
    states = set(uitx.WEAKEST_FIRST)
    assert states == {uitx.UNREGISTERED, uitx.UNCOMPENSATED, uitx.COMPENSATED,
                      uitx.RESTORED, uitx.UNTOUCHED}
    assert len(uitx.WEAKEST_FIRST) == 5
    # `untouched` and `restored` are NOT residue. That is the whole point.
    assert uitx.UNTOUCHED not in uitx.RESIDUE
    assert uitx.RESTORED not in uitx.RESIDUE
    assert uitx.UNCOMPENSATED in uitx.RESIDUE


def test_an_unregistered_inverse_is_weaker_than_no_inverse_at_all() -> None:
    """Item 546's `undeclared`-below-`neither` ordering, and this item's own
    design note: a compensatable step with nothing registered is "the worst of
    the three outcomes: not restored, not reported". `uncompensated` is a
    stated outcome with a stated bound; `unregistered` is an omission, and an
    omission must not inherit the exit of a class that has one."""
    order = uitx.WEAKEST_FIRST
    assert order.index(uitx.UNREGISTERED) < order.index(uitx.UNCOMPENSATED)
    assert uitx.aggregate([uitx.UNREGISTERED, uitx.RESTORED]) \
        == uitx.UNREGISTERED


def test_the_aggregate_is_the_weakest_part_not_the_average() -> None:
    """Item 546 rule 3 (PR #1256), applied to the steps of one transaction. One
    uncompensated click is not diluted by four clean steps."""
    assert uitx.aggregate(
        [uitx.UNTOUCHED, uitx.UNTOUCHED, uitx.RESTORED, uitx.RESTORED,
         uitx.UNCOMPENSATED]) == uitx.UNCOMPENSATED
    assert uitx.aggregate([uitx.UNTOUCHED, uitx.RESTORED]) == uitx.RESTORED
    assert uitx.aggregate([]) is None


def test_a_revert_reports_restored_and_compensated_separately() -> None:
    """The contract. "Rolled back" is not one word covering two outcomes: an
    inverse that puts a field back is a different fact from an offset that
    merely counteracts a crossing, and they land in different lists."""
    split = uitx.revert_report([
        ("read", "screen.observe", False),
        ("type", "ui.text", True),
        ("click", "ui.click", False),
        ("download", "ui.download", False),
    ])
    assert split["untouched"] == ["read"]
    assert split["restored"] == ["type"]
    assert split["compensated"] == []
    assert split["uncompensated"] == ["click", "download"]
    # disjoint by construction: every step appears exactly once
    listed = (split["untouched"] + split["restored"] + split["compensated"]
              + split["uncompensated"] + split["unregistered"])
    assert sorted(listed) == ["click", "download", "read", "type"]


def test_the_claim_refuses_the_word_clean_while_residue_remains() -> None:
    split = uitx.revert_report([("click", "ui.click", False)])
    assert "may not be reported as cleanly reverted" in split["claim"]
    assert "rolled back" not in split["claim"]
    clean = uitx.revert_report([("read", "screen.observe", False)])
    assert "may not be reported" not in clean["claim"]


def test_the_lifo_order_excludes_steps_with_no_inverse() -> None:
    """The compensate phase table read at the transaction level: only the steps
    that have an inverse are in the order, and it is the reverse of the step
    order."""
    split = uitx.revert_report([
        ("first", "ui.text", True),
        ("click", "ui.click", False),
        ("second", "ui.text", True),
    ])
    assert split["compensateOrder"] == ["second", "first"]


def test_a_crossing_this_module_does_not_understand_is_not_relabelled() -> None:
    split = uitx.revert_report([("post", "net.publish", False)])
    assert split["unclassified"] == ["post"]
    assert split["aggregate"] is None


# ------------------------------------------- the measured word, on the report


def test_a_read_is_not_reported_as_residue() -> None:
    """THE MEASUREMENT. Before this change `read_pane` (`screen.observe`, a
    READ) printed `[BARE]` under a note reading "a bare crossing left the
    system with nothing done about it ... so it can be handled out of band".
    Nothing was done about it because there is nothing to do."""
    report = _report(AGENT)
    assert _crossing(report, "read_pane")["uiResidue"] == uitx.UNTOUCHED
    assert _crossing(report, "locate")["uiResidue"] == uitx.UNTOUCHED
    rendered = erase_report.render(report)
    assert "[untouched]    UiAgent  host read_pane()" in rendered
    assert "[BARE]         UiAgent  host read_pane()" not in rendered


def test_a_click_and_a_read_no_longer_share_a_word() -> None:
    """One word covered three outcomes. Two of the three readings were false,
    and an auditor who learns to discount `bare` discounts it everywhere."""
    report = _report(AGENT)
    read = _crossing(report, "read_pane")["uiResidue"]
    click = _crossing(report, "actuate")["uiResidue"]
    download = _crossing(report, "fetch_receipt")["uiResidue"]
    assert read != click
    assert click == download == uitx.UNCOMPENSATED
    # and they were the same word before
    assert _crossing(report, "read_pane")["compensated"] is False
    assert _crossing(report, "actuate")["compensated"] is False


def test_a_declared_inverse_on_a_compensatable_verb_reports_restored() -> None:
    """The other end of the collapse. A `ui.text` inverse puts the field back,
    which is a stronger fact than an offset that merely counteracts."""
    report = _report(AGENT)
    assert _crossing(report, "type_amount")["uiResidue"] == uitx.RESTORED


def test_the_realm_aggregate_is_uncompensated_and_named(
) -> None:
    split = _report(AGENT)["boundaryCrossings"]["uiResidue"]
    assert split["aggregate"] == uitx.UNCOMPENSATED
    assert sorted(split["uncompensated"]) == ["actuate", "fetch_receipt"]
    assert split["restored"] == ["type_amount"]
    assert sorted(split["untouched"]) == ["locate", "read_pane"]
    assert split["compensateOrder"] == ["type_amount"]


def test_a_composition_without_a_ui_verb_is_untouched() -> None:
    """THE CONTROL. Every composition in the tree today crosses no computer-use
    verb; none of them may change. No `uiResidue` section, no `uiTransactions`
    member, and the two-state tags exactly as before."""
    report = _report(NO_UI)
    assert "uiResidue" not in report["boundaryCrossings"]
    assert "uiTransactions" not in report
    assert report["summary"]["uiResidueAggregate"] is None
    for entry in report["boundaryCrossings"]["externs"]:
        assert "uiResidue" not in entry
    rendered = erase_report.render(report)
    assert "[BARE]         PlainAgent  host publish()" in rendered
    assert "item 522" not in rendered


# --------------------------------------------------------- the static plan


def test_the_plan_reads_the_steps_in_source_order() -> None:
    plans = uitx.plans(compile_source(AGENT, "a.rvl"))
    assert len(plans) == 1
    plan = plans[0]
    assert plan["component"] == "UiAgent"
    assert plan["method"] == "settle"
    assert [s["extern"] for s in plan["steps"]] == [
        "read_pane", "locate", "type_amount", "actuate", "fetch_receipt"]


def test_the_plan_names_the_unconfirmed_irreversible_steps() -> None:
    """What stops the absence of a policy rule from being silence. revl does
    not refuse these crossings — it refuses to let them go unnamed."""
    plan = uitx.plans(compile_source(AGENT, "a.rvl"))[0]
    assert plan["unconfirmedIrreversibleSteps"] == ["actuate", "fetch_receipt"]


def test_an_actuation_with_no_following_read_is_unverified() -> None:
    """Design 538 §3.1. A UI actuation's return value says the actuation was
    DELIVERED; it does not say the business effect occurred."""
    plan = uitx.plans(compile_source(AGENT, "a.rvl"))[0]
    assert plan["unverifiedSteps"] == [
        "type_amount", "actuate", "fetch_receipt"]
    states = {s["extern"]: s["postcondition"] for s in plan["steps"]}
    assert states["read_pane"] == uitx.NOT_APPLICABLE
    assert states["actuate"] == uitx.UNVERIFIED


def test_an_actuation_followed_by_a_read_is_verified_against_that_read(
) -> None:
    assert uitx.postcondition("ui.click", followed_by_read=True) \
        == uitx.VERIFIED_AGAINST_UNTRUSTED


def test_there_is_no_plain_verified_verdict() -> None:
    """Design 538 §4: accessibility metadata comes from the application, and a
    hostile or merely broken application is the same input to revl. The
    absence is asserted so a later slice cannot quietly add the stronger word
    without arguing for it."""
    vocabulary = set(uitx.POSTCONDITION_MEANING)
    assert "verified" not in vocabulary
    assert uitx.VERIFIED_AGAINST_UNTRUSTED == "verified-against-untrusted-read"
    for token in ui_family.spellings():
        for followed in (True, False):
            assert uitx.postcondition(token, followed_by_read=followed) \
                != "verified"


# ------------------------------------------------------ the confirmation raise


def test_no_verb_is_born_confirm_required() -> None:
    """Slice 1 asserted the emptiness so a later slice could not ship the label
    without the check. This slice lands the raise and the emptiness STAYS true:
    `confirm-required` is a function of the operator's policy, not a property
    of a verb, so `ui_family.REVERSIBILITY` still has no member in it."""
    assert ui_family.CONFIRM_REQUIRED not in set(
        ui_family.REVERSIBILITY.values())
    assert uitx.raised_class("ui.click", approval_required=False) \
        == ui_family.UNKNOWN
    assert uitx.raised_class("ui.click", approval_required=True) \
        == ui_family.CONFIRM_REQUIRED


def test_the_raise_is_monotone_and_never_weakens() -> None:
    """An operator may raise a verb and may not lower one. Slice 1 kept authors
    from lowering a class; the operator is not exempt from that rule, they are
    only allowed to go the other way."""
    for token in ui_family.spellings():
        assert uitx.raised_class(token, approval_required=True) \
            == ui_family.CONFIRM_REQUIRED
        assert uitx.raised_class(token, approval_required=False) \
            == ui_family.reversibility(token)


def test_a_read_is_not_reported_as_unconfirmed() -> None:
    """The same error this file exists to fix, at the confirmation surface:
    `unconfirmed` must not cover both "nobody authorised an actuation" and
    "there was no actuation"."""
    assert uitx.confirmation("screen.observe", covered=False, raised=False) \
        == uitx.NOT_REQUIRED
    assert uitx.confirmation("ui.click", covered=False, raised=False) \
        == uitx.UNCONFIRMED
    # an operator may still raise a read, and that raise wins
    assert uitx.confirmation("screen.observe", covered=False, raised=True) \
        == uitx.PER_SESSION


def test_a_per_crossing_edge_beats_a_session_gate() -> None:
    assert uitx.confirmation("ui.click", covered=True, raised=True) \
        == uitx.PER_CROSSING


def test_an_activation_body_edge_is_read_as_confirmed_per_crossing() -> None:
    """The positive case, and the reason `plans` reads the activation body: a
    gate that can only ever report one value is not a measurement."""
    plan = uitx.plans(compile_source(CONFIRMED, "c.rvl"))[0]
    assert plan["key"] == "<activation>"
    assert plan["steps"][0]["confirmation"] == uitx.PER_CROSSING
    assert plan["unconfirmedIrreversibleSteps"] == []


def test_the_same_crossing_without_an_edge_is_unconfirmed() -> None:
    plan = uitx.plans(compile_source(UNCONFIRMED, "c.rvl"))[0]
    assert plan["steps"][0]["confirmation"] == uitx.UNCONFIRMED


def test_a_policy_raise_is_read_as_a_session_gate() -> None:
    plan = uitx.plans(compile_source(UNCONFIRMED, "c.rvl"),
                      frozenset({"ui.click"}))[0]
    assert plan["steps"][0]["confirmation"] == uitx.PER_SESSION


# ------------------------------------------- agreement with item 546
#
# Item 546 (PR #1256) landed on main first and settled the general rule these
# five classes fold into. These assertions are the anti-drift check between the
# two vocabularies, so neither item can quietly grow a word the other does not
# have.


def test_the_four_rollback_outcomes_are_spelled_the_same_in_both_items(
) -> None:
    """Item 546's `layer_state.OUTCOMES` is `restored, compensated,
    uncompensated, untouched`, with a note saying `uncompensated` "is item
    522's word for that fact and is taken from it rather than renamed". Those
    four ARE these four, character for character."""
    from revl import layer_state
    assert set(layer_state.OUTCOMES) == {
        uitx.RESTORED, uitx.COMPENSATED, uitx.UNCOMPENSATED, uitx.UNTOUCHED}


def test_the_fifth_state_is_not_a_rollback_outcome_and_is_not_claimed_as_one(
) -> None:
    """`unregistered` has no member in item 546's list and should not: that
    list is what a ROLLBACK DID, and this is a fact about a DECLARATION, known
    before anything runs."""
    from revl import layer_state
    assert uitx.UNREGISTERED not in layer_state.OUTCOMES


def test_every_registry_class_folds_into_item_546s_three() -> None:
    from revl import layer_state
    for token in ui_family.spellings():
        cls = ui_family.reversibility(token)
        state, reason = layer_state.fold("ui", cls)
        assert state in layer_state.STATE_CLASSES
        assert reason


def test_the_raised_class_is_refused_by_the_fold_and_that_is_correct() -> None:
    """Item 546 refuses `confirm-required` as `class-not-a-state-class`: it
    says who may authorise a step, not what the step leaves behind. This slice
    lands the raise and MUST NOT make that refusal wrong, so the raised class
    lives in its own field and never reaches a state-class position."""
    from revl import layer_state
    with pytest.raises(layer_state.PlanRefused) as refused:
        layer_state.fold("ui", ui_family.CONFIRM_REQUIRED)
    assert refused.value.refusal.link == layer_state.CLASS_NOT_A_STATE_CLASS


def test_a_raise_never_moves_a_steps_residue() -> None:
    """The structural half of the same distinction. Raising `ui.text` to
    `confirm-required` changes who must authorise it and leaves `restored`
    exactly where it was."""
    plain = uitx.plans(compile_source(AGENT, "a.rvl"))[0]
    raised = uitx.plans(compile_source(AGENT, "a.rvl"),
                        frozenset(ui_family.spellings()))[0]
    assert [s["residue"] for s in plain["steps"]] \
        == [s["residue"] for s in raised["steps"]]
    assert [s["class"] for s in plain["steps"]] \
        == [s["class"] for s in raised["steps"]]
    assert {s["effectiveClass"] for s in raised["steps"]} \
        == {ui_family.CONFIRM_REQUIRED}


# ------------------------------------------------------------- the gate


def _audit_policy(source: str, policy_text: str) -> list:
    """What `revl audit --policy` now evaluates: the reach gate plus the
    approval-requirement gate."""
    ir = compile_source(source, "gate522.rvl")
    return approval_admission(parse_policy(policy_text, "p.pol"), ir)


def test_audit_policy_refuses_an_unconfirmed_ui_crossing() -> None:
    """THE GATE. `capability ui.click requires approval` is the operator
    raising the verb to `confirm-required`. `approval_admission` has always
    enforced it, and was only ever CALLED from `revl.mcp.session` — so the
    static surface an operator reads before shipping reported this exact
    composition clean and exited 0 while a session refused it. A gate whose
    static surface says clean is a gate an operator learns not to consult."""
    violations = _audit_policy(AGENT, "capability ui.click requires approval\n")
    assert len(violations) == 1
    assert violations[0].kind == "approval"
    assert violations[0].token == "ui.click"
    assert "requires approval" in violations[0].message


def test_the_gate_reaches_a_provide_method_loop() -> None:
    """The shape the item is actually about. `AGENT`'s click is inside a
    `provide` method, which is where a computer-use loop lives."""
    violations = _audit_policy(
        AGENT, "capability ui.download requires approval\n")
    assert [v.token for v in violations] == ["ui.download"]


def test_a_policy_that_raises_nothing_is_still_clean() -> None:
    """CONTROL. Without a rule the gate does not fire — absence of a rule is
    not the gate refusing, it is the gate not being asked."""
    assert _audit_policy(AGENT, "component UiAgent may not reach fs.write\n") \
        == []
    assert _audit_policy(NO_UI, "capability ui.click requires approval\n") == []


def test_an_activation_body_crossing_with_an_edge_admits() -> None:
    """CONTROL. The gate is not a blanket refusal of every UI program: a
    crossing that carries a covering `Approval[ui.click]` edge is admitted
    under the same policy that refuses the one without."""
    assert _audit_policy(
        CONFIRMED, "capability ui.click requires approval\n") == []
    assert len(_audit_policy(
        UNCONFIRMED, "capability ui.click requires approval\n")) == 1


def test_the_gate_names_the_only_acquisition_that_exists() -> None:
    """The refusal has to leave the author somewhere to go, and the honest
    answer today is narrow: `await approval[C]` is allowed only in a component
    activation body. The refusal says so rather than implying a provide-method
    fix that does not exist (docs/design/553-ui-transaction-phases.md §3)."""
    violation = _audit_policy(
        AGENT, "capability ui.click requires approval\n")[0]
    assert "await approval[ui.click]" in violation.message


def test_the_provide_method_cannot_acquire_the_approval_the_gate_wants(
) -> None:
    """THE BLOCKER, re-measured rather than quoted. A computer-use loop lives
    in a `provide` method; `await approval[C]` is refused there, and
    `Approval[C]` cannot be written as a type, so it cannot be threaded in as a
    parameter either. That is why this slice's gate is the OPERATOR's and why
    the unconfirmed steps are named rather than refused."""
    from revl.errors import RevlError
    with pytest.raises(RevlError) as in_method:
        compile_source(
            "extern emission[ui.click] fn actuate(t: Str) = @py { return None }\n"
            "service Ops { emission fn go() }\n"
            "component C provides ops: Ops {\n"
            "  provide ops {\n"
            "    fn go() {\n"
            "      let a = await approval[\"ui.click\"] { t: 1 }\n"
            "      emit actuate(\"Apply\") with a\n"
            "      return\n"
            "    }\n"
            "  }\n"
            "}\n", "blocked.rvl")
    assert "only allowed in a component activation body" in \
        in_method.value.message

    with pytest.raises(RevlError) as as_type:
        compile_source(
            "fn take(a: Approval[ui.click]) -> Int { return 1 }\n", "t.rvl")
    assert "cannot be written as a type" in as_type.value.message

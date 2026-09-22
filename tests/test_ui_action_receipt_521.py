"""The recorded computer-use action (roadmap item 521, issue #1195, Slice 5).

Design 532 §10 asks for "one recorded action carrying application, window,
target role, target name, target evidence hash, action, capability, session,
comparable to item 517's model decision", and names one oracle:

    the receipt for a refused step is as complete as the receipt for a
    successful one, which is item 525's "refusals as legibly as successes"
    applied to one step.

WHAT WAS MISSING, stated as the gap rather than as a feature request. Slice 1
put a UI verb on the G8 audit surface, so `revl audit` says a component can
reach `ui.click`. Slice 4 made the target a record, so a step HAS a binding.
Between them nothing writes down what a particular step did, which means the
audit surface answers "what could this program touch" and nothing answers
"what did it touch". Every other revl step has an answer: an effect has a WAL
record with an inverse, an emission has a deferred record and a flush proof, a
model completion has item 517's signed decision object.

THE ONE RULE, AND THE ARM IT PROTECTS. Every outcome carries every member.
`OUTCOMES` has four arms and none may thin the record. The arm that would be
thinned first is `unresolved`, and it is the one that matters most: design 532
§4.2 refuses the ladder's descent inside one call, so an `unresolved` receipt
is the positive evidence that a `ui.click` which could not bind its target did
NOT become a `ui.click.pixel`. Its members are all fillable - `role`, `name`
and `action` record what was sought, `evidence` records the observation that
was searched - so a producer that thinned it would be dropping facts it had.

SCOPE, stated so nothing here is over-read:
  * nothing signs a receipt, and `test_no_member_claims_authenticity` pins the
    absence. Item 517 signs because the producer and the consumer are both
    inside revl's world; a UI step is performed by the substrate, which design
    532 §7 and item 539 put outside this repository. A signature field revl
    defined and nobody could fill would read as an attestation;
  * nothing here executes a step, and no `@py` body is run;
  * `verify` checks that a member is present and inhabited, never that it
    describes what happened. `evidence` is a hash the host computed and revl
    never sees a screen.

NON-VACUITY. `src/revl/ui_action.py` does not exist on `4cfc8f32`, so every
test here fails at import there. The measurement that is worth stating is the
narrower one: with the module present and the completeness rule weakened to a
presence check (no `EMPTY_ALLOWED` and no unknown-member arm), four of these
tests pass that should not. They are named in
`test_non_vacuity_what_a_presence_check_alone_would_admit`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import ui_action, ui_family  # noqa: E402


# ------------------------------------------------------------------ fixtures

def _target(**overrides) -> dict:
    """A `UiTarget` value as a host would produce it, every registry field
    inhabited. Built from the registry rather than written out, so a field
    added to slice 4's binding arrives here as a KeyError in one place rather
    than as a quietly absent member in every test."""
    filler = {"Str": "x", "Int": 1, "Bool": False}
    value = {name: filler[declared]
             for name, declared in ui_family.TARGET_FIELDS.items()}
    value.update({
        "application": "Billing",
        "window": "Invoice 4471",
        "role": "button",
        "name": "Approve",
        "evidence": "sha256:2b1f...",
        "action": "click",
        "session": "task-88",
        "bounds": "0,0,1440,900",
        "expiry": 1_774_000_000,
        "confirm": True,
    })
    value.update(overrides)
    return value


def _receipt(outcome: str = ui_action.PERFORMED, **overrides) -> dict:
    receipt = ui_action.from_target(
        _target(), component="Billing", step_index=3,
        capability="ui.click", outcome=outcome)
    receipt.update(overrides)
    return receipt


# ------------------------------------------------------- the member vocabulary

def test_the_eight_members_the_design_asks_for_are_present() -> None:
    """Design 532 §10's list, checked by name. `role`, `name` and `evidence`
    are the design's "target role", "target name" and "target evidence
    hash": the receipt does not re-prefix them, because they are copied from
    the target record under the target record's own names and a second
    spelling is a second thing to keep in step."""
    for member in ("application", "window", "role", "name", "evidence",
                   "action", "session", "capability"):
        assert member in ui_action.BODY_MEMBERS, member


def test_the_target_half_is_read_from_the_slice_4_registry() -> None:
    """Not copied. A field added to the binding is a member of the receipt in
    the same change, which is the only way two tables in two modules stay in
    step - and a receipt member the target does not carry and the declaration
    does not name would be a member nothing can fill."""
    assert ui_action.members_from_target() == tuple(ui_family.TARGET_FIELDS)
    assert set(ui_family.TARGET_FIELDS) <= set(ui_action.BODY_MEMBERS)


def test_the_member_types_are_read_from_the_registry_too() -> None:
    for name, declared in ui_family.TARGET_FIELDS.items():
        expected = {"Str": str, "Int": int, "Bool": bool}[declared]
        assert ui_action.MEMBER_TYPES[name] is expected, name


def test_the_crossing_key_is_item_517s_pair() -> None:
    """Reused rather than re-invented, so a UI receipt and a model decision
    index a component's steps the same way. A receipt with no crossing key
    indexes to nothing and cannot be compared against the audit surface that
    named the reach in the first place."""
    assert ui_action.CROSSING_MEMBERS == ("component", "step_index")
    from revl import model_evidence
    assert set(ui_action.CROSSING_MEMBERS) <= set(model_evidence.BODY_MEMBERS)


def test_the_capability_member_is_the_declared_token() -> None:
    """The one member that comes from the DECLARATION rather than from the
    target: the target says what was acted on, the token says under which
    authority. It is also the join key to every surface that already reads a
    capability token, so a receipt is selectable by the same `capability
    <glob>` rule a policy is written with."""
    receipt = _receipt()
    assert receipt[ui_action.CAPABILITY_MEMBER] == "ui.click"
    assert ui_action.verify(_receipt(capability='ui.click(app="Billing")')).ok


def test_every_outcome_arm_carries_a_recorded_meaning() -> None:
    """The text is the rule, not a comment: `verify` quotes the arm list when
    it refuses an unknown one."""
    assert set(ui_action.OUTCOME_MEANING) == set(ui_action.OUTCOMES)
    for arm, meaning in ui_action.OUTCOME_MEANING.items():
        assert meaning.strip(), arm


def test_no_member_claims_authenticity() -> None:
    """Deliberate, and pinned so a later slice cannot add it quietly. Item 517
    carries a MAC because the model host and the promoter are both inside
    revl's world. A UI step is performed by the substrate, which design 532 §7
    and item 539 put outside this repository: revl builds no key for it and
    holds none, so a `signature` member would read as an attestation and would
    not be one. What this module bounds is COMPLETENESS, which is a property
    of the record rather than of the recorder."""
    for word in ("signature", "signer", "mac", "key_id", "envelope"):
        assert word not in ui_action.BODY_MEMBERS, word


# ------------------------------------------------- the rule, and its oracle

def test_a_performed_receipt_verifies() -> None:
    """The control. Every refusal below is this receipt with one thing
    changed."""
    assert ui_action.verify(_receipt()).ok


def test_the_receipt_for_a_refused_step_is_as_complete_as_for_a_performed_one(
) -> None:
    """Design 532 §10's named oracle for this slice, as an equality over the
    member sets rather than as a count. A count would pass for two records
    that carry different members."""
    performed = _receipt(ui_action.PERFORMED)
    refused = _receipt(ui_action.REFUSED)
    assert ui_action.verify(performed).ok
    assert ui_action.verify(refused).ok
    assert set(performed) == set(refused)
    assert set(refused) == set(ui_action.BODY_MEMBERS)


def test_every_arm_is_as_complete_as_every_other() -> None:
    """The oracle generalised over all four arms, so the rule is not one
    comparison that happens to hold between two of them."""
    built = {arm: _receipt(arm) for arm in ui_action.OUTCOMES}
    for arm, receipt in built.items():
        verdict = ui_action.verify(receipt)
        assert verdict.ok, (arm, verdict.reasons)
    assert len({frozenset(r) for r in built.values()}) == 1


def test_the_unresolved_arm_is_the_one_that_records_a_ladder_that_held(
) -> None:
    """The arm a thinning producer would drop first, and the one that matters
    most. Design 532 §4.2 refuses the descent inside one call - a `ui.click`
    that cannot bind its target FAILS and does not become a `ui.click.pixel` -
    so an `unresolved` receipt is the positive evidence that the ladder did not
    fall through. It is complete because its members are fillable: `role`,
    `name` and `action` record what was SOUGHT, and `evidence` records the
    observation that was searched."""
    receipt = _receipt(ui_action.UNRESOLVED)
    assert ui_action.verify(receipt).ok
    assert receipt["name"] == "Approve"
    assert receipt["evidence"]
    assert "did NOT" in ui_action.OUTCOME_MEANING[ui_action.UNRESOLVED]


def test_unresolved_and_failed_are_separate_arms() -> None:
    """A target that bound and then failed is a different fact from one that
    never bound, and the residue a UI transaction (item 522) must assume
    differs between them. One arm for both would make that undecidable from
    the record."""
    assert ui_action.UNRESOLVED != ui_action.FAILED
    assert {ui_action.UNRESOLVED, ui_action.FAILED} <= set(ui_action.OUTCOMES)


# --------------------------------------------------------------- the refusals

def test_a_missing_member_is_refused_by_name() -> None:
    for member in ui_action.BODY_MEMBERS:
        receipt = _receipt()
        del receipt[member]
        verdict = ui_action.verify(receipt)
        assert not verdict.ok, member
        assert any(r == ui_action.RECEIPT_MEMBER and f"`{member}`" in detail
                   for r, detail in verdict.reasons), (member, verdict.reasons)


def test_an_empty_member_is_refused_although_it_is_present() -> None:
    """The check a presence rule cannot make. A member filled with "" passes
    "is it there" and answers nothing, which is how a record drops a fact while
    still looking complete."""
    verdict = ui_action.verify(_receipt(name=""))
    assert not verdict.ok
    assert any(r == ui_action.RECEIPT_EMPTY for r, _ in verdict.reasons)


def test_an_empty_role_is_admitted_and_the_exception_is_the_only_one() -> None:
    """Slice 4 records that an empty `role` is a MEASUREMENT: not every
    platform publishes an accessibility tree, and "" says none was available.
    An ABSENT `role` would say nothing, and a reader could not tell "there was
    no tree" from "nobody looked"."""
    assert ui_action.verify(_receipt(role="")).ok
    assert ui_action.EMPTY_ALLOWED == frozenset({"role"})


def test_a_member_at_the_wrong_type_is_refused() -> None:
    assert not ui_action.verify(_receipt(expiry="soon")).ok
    assert not ui_action.verify(_receipt(step_index="3")).ok
    assert not ui_action.verify(_receipt(confirm="yes")).ok


def test_a_boolean_does_not_pass_as_an_integer() -> None:
    """`True` is an `int` in python, so a confirmation flag landing in a step
    index would pass a naive check silently. Checked because the failure is
    invisible rather than because it is likely."""
    assert not ui_action.verify(_receipt(step_index=True)).ok
    assert not ui_action.verify(_receipt(expiry=True)).ok


def test_an_unknown_outcome_is_refused_and_the_arms_are_named() -> None:
    verdict = ui_action.verify(_receipt("succeeded"))
    assert not verdict.ok
    detail = next(d for r, d in verdict.reasons
                  if r == ui_action.RECEIPT_VOCABULARY)
    for arm in ui_action.OUTCOMES:
        assert f"`{arm}`" in detail, arm


def test_an_unknown_member_is_refused() -> None:
    """The opposite call from slice 4's record check, which ADMITS an unknown
    field, and the two differ because they bound different things: a record is
    the author's own data structure and a receipt is a wire shape between a
    substrate and an auditor. A member nobody named is a member nobody reads,
    so a producer adding one has an extra fact that reaches no consumer while
    looking as though it did."""
    verdict = ui_action.verify(_receipt(pixels="842,611"))
    assert not verdict.ok
    assert any(r == ui_action.RECEIPT_UNKNOWN for r, _ in verdict.reasons)


def test_the_verdict_collects_every_reason() -> None:
    """Collect-all rather than first-failure: a caller repairing a record wants
    the whole list, and one member per attempt turns a malformed record into a
    sequence of round trips."""
    receipt = _receipt("succeeded", name="", pixels="842,611")
    del receipt["window"]
    verdict = ui_action.verify(receipt)
    assert not verdict.ok
    assert {r for r, _ in verdict.reasons} == {
        ui_action.RECEIPT_MEMBER, ui_action.RECEIPT_EMPTY,
        ui_action.RECEIPT_VOCABULARY, ui_action.RECEIPT_UNKNOWN,
    }


def test_every_declared_reason_is_reachable() -> None:
    """A reason nothing raises is a reason nobody has to keep true."""
    raised = set()
    for receipt in (
            {k: v for k, v in _receipt().items() if k != "window"},
            _receipt(expiry="soon"),
            _receipt("succeeded"),
            _receipt(name=""),
            _receipt(pixels="842,611")):
        raised |= {r for r, _ in ui_action.verify(receipt).reasons}
    assert raised == set(ui_action.RECEIPT_REASONS)


# ------------------------------------------------------------- the builder

def test_the_builder_does_not_repair_a_missing_field() -> None:
    """Build and check are separate for item 517's reason: a builder that
    silently filled in a missing member would make the completeness rule
    unobservable, which is the same defect as a report that prints a word it
    cannot support."""
    thin = _target()
    del thin["evidence"]
    receipt = ui_action.from_target(
        thin, component="Billing", step_index=0,
        capability="ui.click", outcome=ui_action.PERFORMED)
    assert "evidence" not in receipt
    assert not ui_action.verify(receipt).ok


def test_the_builder_ignores_a_field_the_registry_does_not_name() -> None:
    """Slice 4 admits an extra field on a `UiTarget` (the record check is a
    floor). It contributes to no receipt member, so the two decisions do not
    fight: the author may carry their own data, and the wire shape stays
    closed."""
    receipt = ui_action.from_target(
        _target(tenant="eu-1"), component="Billing", step_index=0,
        capability="ui.click", outcome=ui_action.PERFORMED)
    assert "tenant" not in receipt
    assert ui_action.verify(receipt).ok


def test_the_record_kind_and_version_are_declared() -> None:
    """A consumer that reads a record without matching on the kind is a
    consumer that will one day read a model decision as a UI action."""
    assert ui_action.RECEIPT_KIND == "revl.ui-action"
    assert ui_action.RECEIPT_VERSION.count(".") == 1


# ------------------------------------------------------------- non-vacuity

def test_non_vacuity_what_a_presence_check_alone_would_admit() -> None:
    """The module does not exist on `4cfc8f32`, so "every test fails there" is
    true and says little. This is the narrower measurement: four receipts that
    a presence-only completeness check admits and this one refuses. Each is a
    record that looks complete and is not.

    They are the reason the rule is "every member present, inhabited, and
    named" rather than "every member present"."""
    presence_only = [
        # a member dropped by being emptied rather than by being removed
        _receipt(name=""),
        _receipt(evidence=""),
        # an outcome outside the closed arm set
        _receipt("succeeded"),
        # a fact the producer carries and no consumer reads
        _receipt(pixels="842,611"),
    ]
    assert len(presence_only) == 4
    for receipt in presence_only:
        assert set(ui_action.BODY_MEMBERS) <= set(receipt)
        assert not ui_action.verify(receipt).ok, receipt

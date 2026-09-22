"""The recorded computer-use action (roadmap item 521, issue #1195, Slice 5).

Design notes: `docs/design/532-typed-computer-use.md` §10, and
`docs/design/565-ui-target-binding.md`, whose §10 says what this slice needs
from slice 4.

A revl execution attaches evidence to every step. An effect carries a WAL
record with an inverse, an emission carries a deferred record and a flush
proof, a boundary crossing carries a receipt, and a model completion carries
the signed decision object of item 517 (`revl.model_evidence`). A computer-use
step carried nothing, which is the gap this module closes: the audit surface
says a component CAN reach `ui.click` (item 521 slice 1, design 532 §6) and
nothing anywhere says what was clicked.

WHAT THE RECEIPT IS
-------------------
One record per UI step: the whole target binding (slice 4's
:data:`revl.ui_family.TARGET_FIELDS`) plus the crossing it belongs to and how
it came out. Design 532 §10 asks for eight members - application, window,
target role, target name, target evidence hash, action, capability, session -
and those eight are here. The receipt carries the REST of the binding too, and
that is a decision rather than a widening: a receipt carrying a SUBSET of the
target's binding recreates, at audit time, exactly the "which field got
dropped" question slice 4 removed at compile time, and it drops it at the
moment somebody is looking.

THE ONE RULE
------------
**Every outcome carries every member.** :data:`OUTCOMES` has four arms and no
arm may thin the record. That is item 525's "refusals as legibly as successes"
applied to one step, and it is the whole oracle design 532 §10 names for this
slice.

The arm that would be thinned first is `unresolved`, and it is the arm that
matters most. Design 532 §4.2 says a `ui.click` that cannot bind its target
FAILS and does not become a `ui.click.pixel`, so an `unresolved` receipt is
the positive evidence that the ladder did NOT descend. Its members are all
fillable: `role`, `name` and `action` record what was SOUGHT, and `evidence`
records the observation that was searched. A record that omitted them would
say "something did not resolve" and leave the operator to guess what.

WHAT REVL DOES NOT CLAIM ABOUT IT
---------------------------------
**It is not signed, and the absence is deliberate.** Item 517's model decision
carries a MAC because the producer (the model host) and the consumer (the
promoter) are both inside revl's world. A UI step is performed by the
computer-use substrate, which design 532 §7 and item 539 put outside this
repository: revl builds no key for it and holds none. A signature field revl
defined and nobody could fill would read as an attestation and would not be
one, which is worse than no field. What this module bounds is COMPLETENESS,
which is a property of the record, not of the recorder.

**It does not make the fields true.** `evidence` is a hash the host computed;
revl never sees a screen. :func:`verify` checks that a member is present and
inhabited, never that it describes what happened.

**Nothing here executes a step.** revl drives no desktop (532 §11).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import ui_family

#: The record kind, in the namespace item 517's `EVIDENCE_KIND` uses. A
#: consumer that reads a record without matching on this is a consumer that
#: will one day read a model decision as a UI action.
RECEIPT_KIND = "revl.ui-action"

#: Schema version. MINOR for an added member, MAJOR for a removed or
#: re-meaning one, the discipline item 522's report module already follows.
RECEIPT_VERSION = "1.0"

#: The crossing the receipt belongs to. Exactly item 517's pair
#: (`model_evidence.crossing_key`), reused rather than re-invented so a UI
#: receipt and a model decision index a component's steps the same way. A
#: receipt with no crossing key indexes to nothing and cannot be compared
#: against the audit surface that named the reach.
CROSSING_MEMBERS: tuple[str, ...] = ("component", "step_index")

#: The declared capability token the step crossed (`ui.click`,
#: `ui.download`). This is the eighth of design 532 §10's list and the one
#: member that comes from the DECLARATION rather than from the target: the
#: target says what was acted on, the token says under which authority. It is
#: also the join key to every other surface that reads a capability token, so
#: a receipt is selectable by the same `capability <glob>` rules a policy is
#: written with.
CAPABILITY_MEMBER = "capability"

#: How the step came out.
PERFORMED = "performed"
REFUSED = "refused"
UNRESOLVED = "unresolved"
FAILED = "failed"

#: The closed outcome vocabulary, with what each arm means. The text is the
#: rule: :func:`verify` quotes it when it refuses an unknown arm, so a reader
#: learns which arm they meant instead of only that theirs is not one.
OUTCOME_MEANING: dict[str, str] = {
    PERFORMED: "the substrate performed the step. This says nothing about "
               "whether the control did what the author expected: a "
               "postcondition is item 522's, and a postcondition read through "
               "a surface the target application controls cannot be stronger "
               "than that surface",
    REFUSED: "the step did not run because something refused it - an operator "
             "approval rule, a capability bound, a budget or session deadline. "
             "This arm carries every member the performed arm does, which is "
             "the point of the record: an auditor asking WHAT was refused "
             "learns as much as one asking what succeeded",
    UNRESOLVED: "the target did not bind. Design 532 §4.2 refuses the descent "
                "inside one call, so this is the positive evidence that the "
                "ladder did NOT fall through to a weaker rung. `role`, `name` "
                "and `action` record what was sought and `evidence` records "
                "the observation that was searched",
    FAILED: "the step ran and the substrate reported it did not complete. "
            "Distinct from `unresolved`, because a target that bound and then "
            "failed is a different fact from one that never bound, and the "
            "residue a UI transaction must assume differs between them",
}

OUTCOMES: tuple[str, ...] = (PERFORMED, REFUSED, UNRESOLVED, FAILED)

#: The member carrying the outcome.
OUTCOME_MEMBER = "outcome"

#: Every member of a receipt, in order, all REQUIRED on every arm. The target
#: half is read from slice 4's registry rather than copied, so the two tables
#: cannot drift: a field added to the binding is a member of the receipt in
#: the same change, and a receipt member that the target does not carry and the
#: declaration does not name would be a member nothing can fill.
BODY_MEMBERS: tuple[str, ...] = (
    CROSSING_MEMBERS
    + (CAPABILITY_MEMBER, OUTCOME_MEMBER)
    + tuple(ui_family.TARGET_FIELDS)
)

#: member -> the python type it carries. `Str` is `str`, `Int` is `int`,
#: `Bool` is `bool`; the target half is derived from the registry's declared
#: types for the same anti-drift reason `BODY_MEMBERS` is.
_REVL_TO_PY: dict[str, type] = {"Str": str, "Int": int, "Bool": bool}

MEMBER_TYPES: dict[str, type] = {
    "component": str,
    "step_index": int,
    CAPABILITY_MEMBER: str,
    OUTCOME_MEMBER: str,
    **{name: _REVL_TO_PY[declared]
       for name, declared in ui_family.TARGET_FIELDS.items()},
}

#: The members a receipt may fill with the empty string, and the only ones.
#:
#: `role` is the accessibility-tree role, and slice 4 records that empty is a
#: MEASUREMENT there: not every platform publishes a tree, and "" says none was
#: available. Every other member must be inhabited, which is what stops a
#: record from dropping a member while still looking complete - the shape a
#: completeness rule has to refuse, because a member filled with "" passes a
#: presence check and answers nothing.
EMPTY_ALLOWED: frozenset[str] = frozenset({"role"})

#: Why a receipt was refused. Stable strings, not G-codes: a verifier refusing
#: a malformed record is not a compiler refusing a program, and reusing a
#: guarantee tag here would put a runtime record on the guarantee surface.
#: This is item 517's own split (`revl.model_evidence`'s `EVIDENCE_*` link
#: names) followed rather than re-argued.
RECEIPT_MEMBER = "member"
RECEIPT_TYPE = "member-type"
RECEIPT_VOCABULARY = "vocabulary"
RECEIPT_EMPTY = "empty-member"
RECEIPT_UNKNOWN = "unknown-member"

RECEIPT_REASONS: tuple[str, ...] = (
    RECEIPT_MEMBER, RECEIPT_TYPE, RECEIPT_VOCABULARY, RECEIPT_EMPTY,
    RECEIPT_UNKNOWN,
)


class Verdict:
    """The outcome of :func:`verify`: ``ok`` only when every check passed, and
    ``reasons`` naming every one that did not, in `BODY_MEMBERS` order.

    Collect-all rather than first-failure, on purpose: a caller repairing a
    record wants the whole list, and reporting one member at a time turns a
    malformed record into a sequence of round trips during which the operator
    learns the shape one field per attempt.
    """

    __slots__ = ("ok", "reasons")

    def __init__(self, reasons: tuple[tuple[str, str], ...] = ()) -> None:
        self.reasons = reasons
        self.ok = not reasons

    def as_dict(self) -> dict:
        return {"ok": self.ok,
                "reasons": [{"reason": r, "detail": d} for r, d in
                            self.reasons]}

    def __repr__(self) -> str:
        if self.ok:
            return "Verdict(ok=True)"
        return f"Verdict(ok=False, reasons={[r for r, _ in self.reasons]!r})"


def members_from_target() -> tuple[str, ...]:
    """The receipt members filled from a `UiTarget` value, which is every
    field slice 4's registry declares. Exposed so a caller can see the join
    rather than infer it from two tables."""
    return tuple(ui_family.TARGET_FIELDS)


def from_target(target: Mapping[str, Any], *, component: str,
                step_index: int, capability: str, outcome: str) -> dict:
    """A receipt for one UI step, from the `UiTarget` value the step carried.

    `target` is the record as the host produced it; its registry fields are
    copied across by name and nothing else is read from it, so a target
    carrying an author's extra field (slice 4 admits those) contributes it to
    no receipt member. `capability` is the DECLARED token, item-294 valuation
    and all, because that is the spelling every other authority surface reads.

    This builds; it does not check. A caller that wants the completeness
    answer calls :func:`verify` on the result, and the two are separate for
    the reason item 517 separates them: a builder that silently repaired a
    missing member would make the completeness rule unobservable.
    """
    receipt = {
        "component": component,
        "step_index": step_index,
        CAPABILITY_MEMBER: capability,
        OUTCOME_MEMBER: outcome,
    }
    for field in ui_family.TARGET_FIELDS:
        if field in target:
            receipt[field] = target[field]
    return receipt


def verify(receipt: Mapping[str, Any]) -> Verdict:
    """Is this a complete receipt?

    Four checks, and the ordering of the reasons is `BODY_MEMBERS` order so two
    verdicts over the same record compare:

    1. every member of :data:`BODY_MEMBERS` is present;
    2. each carries the type :data:`MEMBER_TYPES` declares (`bool` is checked
       before `int`, since `True` is an `int` in python and a confirmation
       flag that passed as a step index would be a silent one);
    3. no member is the empty string unless :data:`EMPTY_ALLOWED` says so;
    4. `outcome` is one of :data:`OUTCOMES`, and no member outside
       :data:`BODY_MEMBERS` is present.

    Check 4's second half is the fail-closed direction for an OPEN record. A
    member nobody named is a member nobody reads, so a producer adding one is
    a producer whose extra fact reaches no consumer while looking as though it
    did. That is the opposite call from slice 4's record check, which admits an
    unknown FIELD, and the two differ because they bound different things: a
    record is the author's data structure and a receipt is a wire shape between
    a substrate and an auditor.
    """
    reasons: list[tuple[str, str]] = []
    for member in BODY_MEMBERS:
        if member not in receipt:
            reasons.append((
                RECEIPT_MEMBER,
                f"`{member}` is missing; every outcome carries every member, "
                f"so a record thinned on one arm is a record an auditor learns "
                f"less from about that arm"))
            continue
        value = receipt[member]
        expected = MEMBER_TYPES[member]
        if expected is bool:
            ok = isinstance(value, bool)
        elif expected is int:
            ok = isinstance(value, int) and not isinstance(value, bool)
        else:
            ok = isinstance(value, expected)
        if not ok:
            reasons.append((
                RECEIPT_TYPE,
                f"`{member}` is {type(value).__name__}, not "
                f"{expected.__name__}"))
            continue
        if (expected is str and value == ""
                and member not in EMPTY_ALLOWED):
            reasons.append((
                RECEIPT_EMPTY,
                f"`{member}` is empty; a member filled with \"\" passes a "
                f"presence check and answers nothing, which is how a record "
                f"drops a fact while still looking complete"))
    outcome = receipt.get(OUTCOME_MEMBER)
    if isinstance(outcome, str) and outcome not in OUTCOMES:
        allowed = ", ".join(f"`{arm}`" for arm in OUTCOMES)
        reasons.append((
            RECEIPT_VOCABULARY,
            f"`{outcome}` is not a recorded outcome; the arms are {allowed}"))
    for member in sorted(set(receipt) - set(BODY_MEMBERS)):
        reasons.append((
            RECEIPT_UNKNOWN,
            f"`{member}` is not a receipt member, so nothing reads it"))
    return Verdict(tuple(reasons))

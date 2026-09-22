"""UI transaction phases, the revert split, the confirmation raise, the LIFO
compensation run and the per-step postcondition (roadmap item 522, issue
#1196, `docs/design/553-ui-transaction-phases.md` and
`docs/design/538-ui-transactions.md` §10, slices 2, 3 and 5).

Slice 1 landed the reversibility classification in `revl.ui_family`: every
computer-use verb carries a registry-owned class, `unknown` is handled exactly
as `irreversible`, and a declaration may neither claim an inverse it does not
have nor omit one it does. That is the half of the item that stops a program
CLAIMING cleanliness. This module is the half that reports what actually
happened, and it exists because of one measured false word.

THE WORD. `revl erase-report` tags every irreversible crossing `bare` and
every crossing with an attached offset `compensated`. Run on the item-525
flagship agent, that prints `[BARE] host read_pane()` beside `[BARE] host
actuate()`, with a note reading "a bare crossing left the system with nothing
done about it ... the report lists it precisely so it can be handled out of
band". `read_pane` is `screen.observe`: a READ. Nothing was done about it
because there is nothing to do, and there is nothing to handle out of band.
`actuate` is `ui.click`: no inverse EXISTS. One word covered three outcomes -
nothing to do, nothing could be done, and nothing was done though an inverse
existed - and two of those three readings are false. A residue report that
over-reports a read is not merely noisy: it is the same defect as one that
under-reports an actuation, because an auditor who learns to discount `bare`
discounts it everywhere.

So the states here are DISJOINT and none of them is a synonym for another:

  untouched      the step changed no state the target owns
  restored       an inverse ran and put the state back
  compensated    an offset was attached that is not a restoration
  uncompensated  no inverse exists; this is residue and it is named as residue
  unregistered   an inverse exists and nobody declared one

The aggregate over a step set is the WEAKEST of its parts, which is item 546's
rule 3 (PR #1256) applied to a step set rather than a layer. `unregistered` is
the weakest, below `uncompensated`, and that ordering is this item's own design
doc talking: a compensatable step with nothing registered is "the worst of the
three outcomes: not restored, not reported"
(`docs/design/538-ui-transactions.md` section 5, R2). It is also item 546's
shape - `undeclared` sits below `neither` there for the same reason, because a
class with no exit must not inherit the exit of a class that has one.

ONE VOCABULARY, NOT TWO. Item 546 landed first and its
`layer_state.OUTCOMES` is `("restored", "compensated", "uncompensated",
"untouched")`, with a note saying `uncompensated` "is item 522's word for that
fact and is taken from it rather than renamed". Those four are these four,
spelled identically, and `tests/test_ui_transaction_phases_522.py` asserts the
agreement so the two cannot drift. The fifth, `unregistered`, has no member
there and should not: 546's list is what a ROLLBACK DID, and `unregistered` is
a fact about a DECLARATION that is known before anything runs.

`confirm-required` is likewise NOT folded into any of them. Item 546 refuses
that fold by name (`class-not-a-state-class`) because the class says who may
authorise a step rather than what the step leaves behind, and this module keeps
the distinction structurally: a step carries its registry state class and its
raised authority class in SEPARATE fields, and the residue verdict is computed
from the state class alone, so a raise can never move a step's residue.

FAILURE DIRECTION, stated for every decision in this module:

  * the aggregate is the weakest part, so one unclassifiable step cannot be
    averaged away by four clean ones;
  * an unrecognised token has NO residue state here rather than a permissive
    one - this module never invents a verdict for a crossing it does not
    understand, it declines to speak and the caller's existing `bare` stands;
  * `unknown` and `irreversible` both reach `uncompensated`, which is
    `ui_family.NO_INVERSE`'s join and not a second copy of it;
  * a postcondition is never reported as plain `verified`. The strongest word
    this module prints is `verified-against-untrusted-read`, because the read
    it was checked with came from the application under test
    (`docs/design/538-ui-transactions.md` section 4).

THE POSTCONDITION IS BOUND TO ITS STEP (slice 5, issue #1370). The verdict
used to be POSITIONAL: any later reversible crossing in the same method made
every earlier actuation `verified-against-untrusted-read`. That is the sentence
"a read follows this actuation", printed as though it were "a read checks this
actuation", and a program satisfies it by clicking Approve and reading Cancel.
`_checked_by` requires three things instead of one: the read comes after the
actuation, it resolves THE SAME TARGET by provenance, and every crossing it
derives from is later than the actuation. A read that follows and checks
something else gets its own word, `read-not-bound-to-this-step`, because the
alternative is one word covering two outcomes, which is the defect the residue
states above exist to fix.

WHAT THIS MODULE DOES NOT DO. It performs nothing. There is no runtime
transaction and no phase scheduler; the phases below are a COMPILE-TIME
ELIGIBILITY decision (which phases a step of a given class may occupy) and a
static plan read off the lowered IR. `compensation_run` (slice 3, issue #1369)
COMPUTES a LIFO run - its order, its membership and each step's outcome, keyed
on the step the transaction failed at - and the compensating crossings are
performed by the computer-use substrate, roadmap item 539, upstream
`inso1337/revl-harness#11`, which is what performs the actuations too. Saying
so here is the point of section 3 of the slice-1 design: an implied guarantee
revl does not enforce is worse than no phase list at all.

WHAT STAYS OPEN, so a reader of this module does not infer it closed. The
CHECK-TO-USE RACE (issue #1371) is not closed here. Item 521 slice 4 carries a
`UiTarget` by value and not as a resolved handle, so the binding above is
re-resolution by name: two resolutions of the same name at two instants may
return two different controls, and `docs/design/565-ui-target-binding.md` §7
says revl "checks the signature, not the dataflow between two crossings". A
step reports the `ui.find` its target came from (`targetResolvedBy`) and
declines to bind at all when an extern declares two `UiTarget` parameters,
which is §7's undecided question answered on the fail-closed side. Neither is
the race, and neither should be read as it.
"""

from __future__ import annotations

from . import ui_family

DESIGN = "docs/design/553-ui-transaction-phases.md"

# --------------------------------------------------------------- the phases
#
# The eight the item proposes, in order. Writing the list down is not the
# design; the design is the `OWNER` and `DECIDES` tables below, which say for
# each phase who runs it and what revl - which does not hold the target's
# state - is entitled to decide about it.

OBSERVE = "observe"
PLAN = "plan"
STAGE = "stage"
PREVIEW = "preview"
CONFIRM = "confirm"
COMMIT = "commit"
VERIFY = "verify"
COMPENSATE = "compensate"

PHASES: tuple[str, ...] = (
    OBSERVE, PLAN, STAGE, PREVIEW, CONFIRM, COMMIT, VERIFY, COMPENSATE)

#: Who runs the phase. `substrate` is the host computer-use driver (item 539);
#: `operator` is the item-246 approval authority; `revl` is this compiler.
OWNER: dict[str, str] = {
    OBSERVE: "substrate",
    PLAN: "substrate",
    STAGE: "substrate",
    PREVIEW: "substrate",
    CONFIRM: "operator",
    COMMIT: "substrate",
    VERIFY: "substrate",
    COMPENSATE: "revl",
}

#: What revl decides about the phase. `None` means: nothing. An honest nothing
#: is the entry that keeps this table from being a promise.
DECIDES: dict[str, str | None] = {
    OBSERVE: "the result is attacker-influenced content and never a trusted "
             "reference (item 521, docs/design/532-typed-computer-use.md §5)",
    PLAN: None,
    STAGE: None,
    PREVIEW: None,
    CONFIRM: "whether the crossing is confirmable at all, and by which "
             "authority - see `confirmation` below",
    COMMIT: "which verbs may be crossed, by the existing G4 subset rule and "
            "the G8 reach",
    VERIFY: "that a postcondition read is no stronger than the read it came "
            "from",
    COMPENSATE: "that the registered compensations MATCH the registry class: "
                "present where an inverse exists, absent where none does "
                "(Slice 1, docs/design/538-ui-transactions.md §5)",
}

#: class -> the phases a step of that class may OCCUPY. This is the compile-
#: time half of the phase list and the reason the list is worth writing down.
#:
#: The load-bearing rows are the absences, not the presences:
#:
#:   * a `reversible` verb is eligible for `verify` and NOT for `commit` or
#:     `compensate`. It actuates nothing, so it adds nothing a transaction has
#:     to undo, which is exactly why a `verify` phase built out of a fresh
#:     `screen.observe` / `ui.find` pair does not grow the residue it is
#:     checking (design 538 §3.1);
#:   * an `irreversible` or `unknown` verb is NOT eligible for `compensate`.
#:     That is the whole item in one table cell: a transaction may not place a
#:     step with no inverse in the phase whose name means it was undone.
ELIGIBLE: dict[str, tuple[str, ...]] = {
    ui_family.REVERSIBLE: (OBSERVE, PLAN, PREVIEW, VERIFY),
    ui_family.COMPENSATABLE: (STAGE, PREVIEW, COMMIT, COMPENSATE),
    ui_family.CONFIRM_REQUIRED: (STAGE, PREVIEW, CONFIRM, COMMIT),
    ui_family.IRREVERSIBLE: (STAGE, PREVIEW, CONFIRM, COMMIT),
    ui_family.UNKNOWN: (STAGE, PREVIEW, CONFIRM, COMMIT),
}


def eligible_phases(token: str) -> tuple[str, ...]:
    """The phases a computer-use step of `token`'s registry class may occupy.
    `()` for a token that is not a computer-use verb - this module declines to
    speak about crossings it does not own."""
    cls = ui_family.reversibility(token)
    return ELIGIBLE.get(cls, ()) if cls is not None else ()


def may_compensate(token: str) -> bool:
    """Whether a transaction may place this step in the `compensate` phase.
    False for `irreversible` and `unknown`, which is `ui_family.NO_INVERSE`
    read through the phase table rather than a second copy of it."""
    return COMPENSATE in eligible_phases(token)


# ------------------------------------------------------- the residue states
#
# Five disjoint states. None is a synonym for another and none is a range.

UNTOUCHED = "untouched"
RESTORED = "restored"
COMPENSATED = "compensated"
UNCOMPENSATED = "uncompensated"
UNREGISTERED = "unregistered"

#: What each state asserts, in the words the report prints. This is the rule,
#: not a comment: the renderers quote it.
MEANING: dict[str, str] = {
    UNTOUCHED: "the step changed no state the target owns, so there is "
               "nothing to restore and nothing left behind - this is not "
               "residue and a report that counts it as residue is wrong",
    RESTORED: "an inverse exists, was declared, and puts the state the step "
              "changed back the way it was",
    COMPENSATED: "an offset was attached. It is a second crossing chosen to "
                 "counteract the first, not a restoration: the first crossing "
                 "still happened and anything that already observed it still "
                 "observed it (paper §6.1)",
    UNCOMPENSATED: "no inverse exists. This is residue, it is named as "
                   "residue, and no run of any compensation set changes that",
    UNREGISTERED: "an inverse exists and nobody declared one. Residue that "
                  "was avoidable and that the report cannot even name - the "
                  "worst of the outcomes (docs/design/538-ui-transactions.md "
                  "§5, R2)",
}

#: WEAKEST FIRST. `aggregate` returns the first member present in a step set,
#: which is item 546's rule 3 (a layer's aggregate is the weakest of its
#: parts, PR #1256) applied to the steps of one transaction.
#:
#: `unregistered` is below `uncompensated` for item 546's own reason:
#: `uncompensated` is a stated outcome with a stated bound ("no inverse
#: exists"), while `unregistered` is an omission, and an omission must not
#: inherit the exit of a class that has one. Item 546 orders `undeclared`
#: below `neither` by exactly this argument.
WEAKEST_FIRST: tuple[str, ...] = (
    UNREGISTERED, UNCOMPENSATED, COMPENSATED, RESTORED, UNTOUCHED)

#: The states that are RESIDUE - the ones an auditor must handle out of band.
#: `untouched` and `restored` are deliberately absent.
RESIDUE: frozenset[str] = frozenset({UNREGISTERED, UNCOMPENSATED, COMPENSATED})


def residue_state(token: str, has_compensate: bool) -> str | None:
    """The residue state of one computer-use crossing, or `None` when `token`
    is not a computer-use verb.

    `None` rather than a default is the failure direction: this module does not
    invent a verdict for a crossing it does not understand, and the caller's
    existing tagging stands untouched for every non-UI crossing. That keeps a
    report over a composition with no computer-use verb byte-identical.
    """
    cls = ui_family.reversibility(token)
    if cls is None:
        return None
    if cls == ui_family.REVERSIBLE:
        return UNTOUCHED
    if cls == ui_family.COMPENSATABLE:
        # Slice 1 refuses the declaration that omits the inverse, so
        # `unregistered` is unreachable from a program that admits TODAY. It is
        # written down anyway, because the state has to exist for the ordering
        # above to be the one the design argues, and because a recorded IR from
        # before that refusal landed still deserves a truthful word.
        return RESTORED if has_compensate else UNREGISTERED
    # irreversible and unknown: `ui_family.NO_INVERSE`, joined for the decision
    # and separated only for the diagnostic.
    return UNCOMPENSATED


def aggregate(states) -> str | None:
    """The weakest state in `states`, or `None` for an empty set. Item 546
    rule 3: the aggregate of a set of parts is its weakest part, so one
    unclassifiable step cannot be averaged away by four clean ones."""
    present = {s for s in states if s in WEAKEST_FIRST}
    for state in WEAKEST_FIRST:
        if state in present:
            return state
    return None


def revert_report(steps) -> dict:
    """The revert split. `steps` is an iterable of
    `(label, token, has_compensate)`.

    The contract this function exists to keep, and the reason it returns five
    lists rather than a count: A REVERT REPORTS WHAT IT RESTORED AND WHAT IT
    ONLY COMPENSATED SEPARATELY. "Rolled back" is not one word covering two
    outcomes, and `bare` is not one word covering three. Each step appears in
    exactly one list, the lists are disjoint by construction, and `aggregate`
    is the weakest state present.

    `compensateOrder` is the LIFO order the compensations WOULD run in - the
    reverse of the step order, restricted to the steps that have one. It is a
    plan, not an execution: nothing in revl runs it (item 539 owns the
    substrate), and calling it an order rather than a result is the honest
    word for a static artifact."""
    buckets: dict[str, list] = {state: [] for state in WEAKEST_FIRST}
    ordered: list[tuple[str, str, str]] = []
    unclassified: list[str] = []
    for label, token, has_compensate in steps:
        state = residue_state(token, has_compensate)
        if state is None:
            unclassified.append(label)
            continue
        buckets[state].append(label)
        ordered.append((label, token, state))
    residue_steps = [label for label, _t, state in ordered
                     if state in RESIDUE]
    return {
        # the five disjoint lists, each under its own name
        "untouched": buckets[UNTOUCHED],
        "restored": buckets[RESTORED],
        "compensated": buckets[COMPENSATED],
        "uncompensated": buckets[UNCOMPENSATED],
        "unregistered": buckets[UNREGISTERED],
        # not a computer-use verb: this module says nothing about it
        "unclassified": unclassified,
        "aggregate": aggregate(state for _l, _t, state in ordered),
        # LIFO: the reverse of the step order, restricted to steps that have an
        # inverse to run. A step with no inverse is NOT in this list, which is
        # the phase table's `compensate` row read at the transaction level.
        "compensateOrder": [label for label, token, _s in reversed(ordered)
                            if may_compensate(token)],
        "residueSteps": residue_steps,
        # the one sentence a transaction over these steps may say about itself
        "claim": _claim(buckets),
    }


def _claim(buckets: dict[str, list]) -> str:
    """The sentence the transaction is entitled to. It never contains the word
    `clean` while anything is uncompensated or unregistered, and it never says
    `rolled back` at all - restoration and compensation are reported by their
    own names or not at all."""
    restored = len(buckets[RESTORED])
    compensated = len(buckets[COMPENSATED])
    uncompensated = len(buckets[UNCOMPENSATED])
    unregistered = len(buckets[UNREGISTERED])
    untouched = len(buckets[UNTOUCHED])
    parts = []
    if restored:
        parts.append(f"{restored} restored")
    if compensated:
        parts.append(f"{compensated} offset without restoration")
    if uncompensated:
        parts.append(f"{uncompensated} uncompensated (no inverse exists)")
    if unregistered:
        parts.append(f"{unregistered} unregistered (an inverse exists and none "
                     f"was declared)")
    if untouched:
        parts.append(f"{untouched} untouched (a read; not residue)")
    if not parts:
        return "no computer-use step in this set"
    if uncompensated or unregistered:
        return ("residue remains: " + ", ".join(parts)
                + ". This set may not be reported as cleanly reverted")
    return ", ".join(parts)


# --------------------------------------------- the LIFO compensation run
#
# Roadmap item 522 slice 3 (issue #1369), design 538 §10. `revert_report`
# above produces `compensateOrder`, which is the reverse of the step order
# restricted to the steps that HAVE an inverse. That is an order, and an order
# is not a run: it says nothing about WHEN the transaction stopped, so it names
# the compensations of steps that never executed. Measured on the five-step
# oracle in `tests/test_ui_transaction_run_1369.py`: with the third step
# failing, `compensateOrder` is three labels long and two of them belong to
# steps four and five, which the failure means never ran.
#
# A run is keyed on the step that failed. Three rules, each with its direction:
#
#   * a step AFTER the failure never executed, so its residue is `untouched`.
#     It is not `restored` (nothing was undone because nothing was done) and it
#     is not residue at all;
#   * the FAILING step's own compensation DOES run when one is registered. The
#     postcondition being unmet says revl could not see the effect land, which
#     is not the same as knowing it did not: a registered inverse that puts a
#     field back is correct whether or not the typing arrived, and skipping it
#     would leave residue on the reading that the step DID land. 538 §10's
#     oracle comes out the same either way, because its third step is a
#     `ui.click`, and stating the rule is what stops the next reader from
#     having to guess;
#   * every step at or before the failure keeps `residue_state`'s verdict, so
#     a step with no inverse reports `uncompensated` after the run exactly as
#     it did before it. No run of any compensation set changes that, and this
#     function does not print a word that suggests otherwise.
#
# WHAT THIS RUNS. Nothing. revl computes the run - the order, the membership
# and the per-step outcome - and the compensating CROSSINGS are performed by
# the computer-use substrate (roadmap item 539, upstream
# `inso1337/revl-harness#11`), exactly as the actuations are. `performedBy`
# says so in the artifact rather than only here. One consequence is stated and
# not engineered away: a compensation that is performed and FAILS has no word
# in the five-state vocabulary above (it was registered, so not
# `unregistered`; it did not put the state back, so not `restored`), and this
# module does not invent one for an execution it does not witness.

#: The sentence the run's artifact carries about who performs it.
PERFORMED_BY = ("revl computes this run - its order, its membership and its "
                "per-step outcome. The compensating crossings are performed "
                "by the computer-use substrate (roadmap item 539, upstream "
                "inso1337/revl-harness#11), which is also what performs the "
                "actuations. Nothing in revl drives a desktop.")

#: Why a step at or before the failure has no compensation in the run.
NO_COMPENSATION_REASON: dict[str, str] = {
    ui_family.REVERSIBLE: "a read; it changed nothing, so there is nothing to "
                          "undo",
    ui_family.COMPENSATABLE: "an inverse exists and none was declared, so the "
                             "run has nothing to call",
    ui_family.IRREVERSIBLE: "no inverse exists; this step's effect stands and "
                            "is reported as residue",
    ui_family.UNKNOWN: "revl cannot tell whether an inverse exists, which is "
                       "handled exactly as though none does",
}


def compensation_run(steps, failed_at) -> dict:
    """The LIFO compensation run for one transaction, given the step it failed
    at. `steps` is an iterable of `(label, token, has_compensate)`, the same
    shape `revert_report` takes; `failed_at` is a step's label or its index
    among the computer-use steps.

    This is item 522's first exit clause: a mid-transaction failure runs the
    registered compensations in order, and a step with no inverse reports
    `uncompensated` rather than a clean teardown. `ran` is the LIFO order, and
    it stops at the failure rather than covering the whole method.

    Raises `LookupError` for a `failed_at` that names no step. A run over a
    failure this module cannot locate would be a run over the whole
    transaction, which is the fail-open answer.
    """
    classified: list[tuple[str, str, bool, str]] = []
    for label, token, has_compensate in steps:
        state = residue_state(token, has_compensate)
        if state is None:
            continue
        classified.append((label, token, has_compensate, state))
    labels = [label for label, _t, _c, _s in classified]
    if isinstance(failed_at, str):
        if failed_at not in labels:
            raise LookupError(
                f"no computer-use step named {failed_at!r} in this "
                f"transaction: {labels}")
        index = labels.index(failed_at)
    else:
        index = int(failed_at)
        if not 0 <= index < len(classified):
            raise LookupError(
                f"step index {index} is outside this transaction's "
                f"{len(classified)} computer-use steps")

    ran: list[str] = []
    not_run: list[dict] = []
    # A LIST and not a map from label to outcome: two steps of one transaction
    # may cross the same extern, and a map would report one of them twice and
    # the other never. The step order is the transaction's own.
    outcomes: list[dict] = []
    for position, (label, token, has_compensate, state) in \
            enumerate(classified):
        # `actuation` separates the steps a reader has to think about from the
        # reads. A read that never executed is not news; an actuation that
        # never executed is the difference between this run and the order it
        # replaces.
        actuation = ui_family.reversibility(token) != ui_family.REVERSIBLE
        if position > index:
            # never executed: the transaction stopped before it
            outcomes.append({"step": label, "outcome": UNTOUCHED,
                             "executed": False, "actuation": actuation})
            continue
        outcomes.append({"step": label, "outcome": state, "executed": True,
                         "actuation": actuation})
        if may_compensate(token) and has_compensate:
            continue
        cls = ui_family.reversibility(token)
        not_run.append({
            "step": label,
            "reason": NO_COMPENSATION_REASON.get(
                cls, "this module has no verdict for that class"),
        })
    for position in range(index, -1, -1):
        label, token, has_compensate, _state = classified[position]
        if may_compensate(token) and has_compensate:
            ran.append(label)

    return {
        "failedStep": classified[index][0],
        "failedStepIndex": index,
        # LIFO, from the failing step back to the first
        "ran": ran,
        "notRun": not_run,
        "neverExecuted": labels[index + 1:],
        "outcomes": outcomes,
        "aggregate": aggregate(entry["outcome"] for entry in outcomes),
        "claim": _run_claim(classified, index, ran),
        "performedBy": PERFORMED_BY,
    }


def _run_claim(classified, index: int, ran: list[str]) -> str:
    """The sentence this run is entitled to. It names the step it stopped at,
    never says `rolled back`, and refuses the word `clean` while any step at or
    before the failure has no inverse."""
    failed = classified[index][0]
    residue = [label for label, token, has_compensate, state
               in classified[:index + 1]
               if state in RESIDUE and not (may_compensate(token)
                                            and has_compensate)]
    head = (f"the transaction stopped at `{failed}`; "
            + (f"{len(ran)} registered compensation"
               f"{'' if len(ran) == 1 else 's'} run, LIFO"
               if ran else "no registered compensation runs"))
    if residue:
        return (head + ". Residue no inverse describes remains at "
                + ", ".join(f"`{label}`" for label in residue)
                + ". This transaction may not be reported as cleanly reverted")
    return head


# --------------------------------------------------- the confirmation raise
#
# `confirm-required` is the class a token is RAISED to by the operator's
# existing approval authority (item 246). Slice 1 asserted that NO VERB IS BORN
# IN IT and made that emptiness a test, so that a later slice could not ship
# the label without the raise. This is the raise.
#
# It is a FUNCTION of the registry class and the operator's policy, not an
# entry in `ui_family.REVERSIBILITY`. That is deliberate and it is what keeps
# slice 1's emptiness test true: the table still has no `confirm-required`
# member, because the class is not a property of the verb.

#: The emit carries a covering `with a` edge, where `a` came from
#: `await approval[C] { ... }`. The only unforgeable confirmation revl has.
PER_CROSSING = "confirmed-per-crossing"
#: The operator's policy raises the token and admission enforces coverage. A
#: SESSION gate: it binds the composition, not the individual click.
PER_SESSION = "confirmed-per-session"
#: Nothing requires or supplies a confirmation for this crossing.
UNCONFIRMED = "unconfirmed"
#: A read. There is no actuation for a human to authorise, so `unconfirmed`
#: would be this module committing the very error it exists to fix: one word
#: covering "nobody authorised an actuation" and "there was no actuation".
#: An operator may still RAISE a read (observing a screen is itself an act),
#: and that raise wins over this row.
NOT_REQUIRED = "confirmation-not-required"

CONFIRMATION_MEANING: dict[str, str] = {
    NOT_REQUIRED: "a read actuates nothing, so no authority is being asked to "
                  "stand behind an actuation here. An operator may still raise "
                  "the verb - observing a screen is itself an act - and that "
                  "raise is reported instead of this",
    PER_CROSSING: "this crossing carries an `Approval[C]` edge minted by "
                  "`await approval[C]` in the activation body. Per-crossing "
                  "and unforgeable (item 246)",
    PER_SESSION: "the operator's policy raises this capability to "
                 "`confirm-required`; admission refuses the composition unless "
                 "the reach is covered. A SESSION gate - it binds what the "
                 "composition may reach, not which individual actuation a "
                 "human saw",
    UNCONFIRMED: "no authority requires a confirmation for this crossing and "
                 "none is supplied. The step still crosses; this is the state "
                 "the item's confirmation gate exists to make visible, and "
                 "naming it is what stops the absence from being silence",
}


def raised_class(token: str, *, approval_required: bool) -> str | None:
    """The EFFECTIVE class of `token` once the operator's authority is read:
    `ui_family.CONFIRM_REQUIRED` when a policy rule raises it, the registry
    class otherwise, `None` when the token is not a computer-use verb.

    The raise is monotone by construction - it only ever strengthens the
    obligation - so an operator can never move a verb to a weaker class. That
    is the same rule slice 1 applied to authors (a classification anyone can
    lower is one a careless party lowers); the operator is not exempt from it,
    they are simply allowed to go the other way."""
    cls = ui_family.reversibility(token)
    if cls is None:
        return None
    return ui_family.CONFIRM_REQUIRED if approval_required else cls


def confirmation(token: str, *, covered: bool, raised: bool) -> str | None:
    """The confirmation state of one crossing, or `None` when `token` is not a
    computer-use verb.

    `covered` - the `emit` carries a covering `with a` approval edge.
    `raised`  - the operator's policy names this token in a `requires approval`
                rule.

    The per-crossing edge wins when both hold: a session gate that also has a
    per-crossing edge is confirmed per crossing, and reporting the weaker of
    the two would understate what the program actually did."""
    cls = ui_family.reversibility(token)
    if cls is None:
        return None
    if covered:
        return PER_CROSSING
    if raised:
        return PER_SESSION
    if cls == ui_family.REVERSIBLE:
        return NOT_REQUIRED
    return UNCONFIRMED


# ------------------------------------------------------------ postconditions
#
# Design 538 §3.1: a UI actuation's return value says the actuation was
# DELIVERED. It does not say the business effect occurred - a click can land on
# a re-rendered control, on a disabled button that swallows it, or on the right
# button in an application that then failed server-side. So a postcondition is
# a fresh `screen.observe` / `ui.find` read, never a return code.
#
# Design 538 §4 then bounds what that read is worth: accessibility metadata
# comes from the application, and a hostile or merely broken application is the
# same input to revl. So the strongest word available is not `verified`.

#: A read pair BOUND TO THIS STEP follows the actuation. The strongest word
#: this module prints.
VERIFIED_AGAINST_UNTRUSTED = "verified-against-untrusted-read"
#: A read follows the actuation and does not check it. Slice 5's word, and the
#: reason slice 5 exists: `unverified` and `verified-against-untrusted-read`
#: were the whole vocabulary, so a read of something else had to be reported as
#: one of them, and both readings are false.
UNBOUND = "read-not-bound-to-this-step"
#: The actuation is followed by no read that could bear a postcondition.
UNVERIFIED = "unverified"
#: A read commits nothing, so there is no postcondition to hold it to.
NOT_APPLICABLE = "not-applicable"

POSTCONDITION_MEANING: dict[str, str] = {
    VERIFIED_AGAINST_UNTRUSTED:
        "a fresh `screen.observe` / `ui.find` read follows this actuation, "
        "resolves THE TARGET THIS STEP ACTED ON, and reads it from an "
        "observation taken after the actuation, so it can carry this step's "
        "postcondition. The read came from the application under test, so it "
        "RAISES CONFIDENCE and does not establish a fact - there is "
        "deliberately no plain `verified` in this vocabulary "
        "(docs/design/538-ui-transactions.md §4)",
    UNBOUND:
        "a read follows this actuation and nothing binds it to this step: it "
        "resolves a DIFFERENT target, or it reads an observation taken BEFORE "
        "the actuation, or this step's target cannot be identified because its "
        "extern declares more than one `UiTarget` parameter and a capability "
        "token carries no parameter roles "
        "(docs/design/565-ui-target-binding.md §7). Position is not a "
        "postcondition: `unverified` would say no read follows, which is "
        "false, and the verified word would credit this step with a check of "
        "something else",
    UNVERIFIED:
        "this actuation is followed by no read. Its return value says it was "
        "delivered, which is not the same claim as the business effect having "
        "occurred (docs/design/538-ui-transactions.md §3.1)",
    NOT_APPLICABLE:
        "a read actuates nothing, so it has no postcondition of its own",
}

#: The four postcondition words, strongest first. `POSTCONDITION_MEANING` has
#: these keys exactly, and `tests/test_ui_postcondition_binding_1370.py`
#: asserts the equality rather than trusting it.
POSTCONDITION_STATES: tuple[str, ...] = (
    VERIFIED_AGAINST_UNTRUSTED, UNBOUND, UNVERIFIED, NOT_APPLICABLE)

#: The states that leave an actuation WITHOUT a postcondition. A failure at a
#: step in this set is a failure the transaction never learns about, which is
#: what ties slice 5 to slice 3's compensation run: a LIFO run is triggered by
#: an unmet postcondition, so a step with no postcondition triggers nothing.
NO_POSTCONDITION: frozenset[str] = frozenset({UNBOUND, UNVERIFIED})


def postcondition(token: str, *, checked_by_read: bool,
                  followed_by_read: bool = False) -> str | None:
    """The postcondition state of one crossing, or `None` for a non-UI token.

    `checked_by_read` - a later read is BOUND to this step: it resolves the
    target this step acted on, from an observation taken after it. This is the
    fact slice 5 added and it is the only one that reaches the verified word.

    `followed_by_read` - a later read exists at all, anywhere in the method.
    It only chooses between `unverified` (no read follows) and `unbound` (one
    does and it checks something else), and it defaults to False so a caller
    that does not state it gets the word that claims less about reads it never
    mentioned.

    Note what is NOT here: there is no argument by which this function returns
    plain `verified`. A postcondition read through a surface the target
    application controls cannot be stronger than that surface. And there is no
    argument by which POSITION alone reaches the verified word, which is what
    slice 5 (issue #1370) changed: the parameter that decides it is a binding
    and not an index."""
    cls = ui_family.reversibility(token)
    if cls is None:
        return None
    if cls == ui_family.REVERSIBLE:
        return NOT_APPLICABLE
    if checked_by_read:
        return VERIFIED_AGAINST_UNTRUSTED
    return UNBOUND if followed_by_read else UNVERIFIED


# ------------------------------------------------- the static plan over IR
#
# The phase list, read off a lowered composition. One plan per provide method
# that crosses at least one computer-use verb: the ordered steps, each step's
# class, eligible phases, residue state, confirmation state and postcondition
# state, plus the LIFO compensation order and the weakest-part aggregate.
#
# This is a READING, not an execution. It runs nothing and schedules nothing.


def _target_positions(entry: dict) -> tuple[int, ...]:
    """The parameter positions this emission extern declares as `UiTarget`.

    Item 521 slice 4 refuses an actuation extern that declares none, so an
    admitted program gives exactly one here for every actuation. TWO is the
    case `docs/design/565-ui-target-binding.md` §7 names as undecided: a
    capability token carries no parameter roles, so the check says a parameter
    is a target and not which one. This function reports what it found and the
    caller declines to bind rather than picking one, which is the fail-closed
    side: a guessed target would make the postcondition verdict wrong in
    exactly the direction slice 5 exists to fix.
    """
    return tuple(
        position for position, param in enumerate(entry.get("params") or [])
        if ui_family.strip_qualifiers_shallow(param.get("type"))
        == ui_family.TARGET_TYPE)


def _extern_index(ir: dict) -> dict:
    """extern name -> what this module needs to know about that emission
    extern: its capability token, whether it declares a compensation, the name
    of the declared inverse, and which of its parameters is the target. A
    token per extern; the first declared capability is the one the G8 audit
    surface names for it."""
    index: dict[str, dict] = {}
    for entry in ir.get("externs") or []:
        if entry.get("class") != "emission":
            continue
        caps = entry.get("capabilities") or []
        compensate = entry.get("compensate")
        callee = (compensate or {}).get("callee") or {}
        index[entry.get("name")] = {
            "token": caps[0] if caps else entry.get("name"),
            "compensate": compensate is not None,
            "inverse": callee.get("name"),
            "targets": _target_positions(entry),
        }
    return index


def _calls(node) -> list[tuple[str, bool, list]]:
    """Every `(extern name, carries an approval edge, argument list)` a
    statement calls, in source order. Walks `let`, `emit` and plain expression
    statements; an `emit ... with a` records its edge so `confirmation` can
    read it.

    The arguments are carried because slice 5 (issue #1370) binds a
    postcondition to the TARGET a step acted on, and the target is an argument.
    """
    found: list[tuple[str, bool, list]] = []
    if not isinstance(node, dict):
        return found
    step = node.get("step")
    approved = bool(node.get("approval"))
    expr = None
    if step == "emit":
        expr = node.get("expr")
    elif step == "let":
        expr = node.get("value")
    elif step == "expr":
        expr = node.get("expr")
    if isinstance(expr, dict) and expr.get("kind") == "fn" and expr.get("name"):
        found.append((expr["name"], approved, list(expr.get("args") or [])))
    return found


# ------------------------------------------- what a step's target IS
#
# Roadmap item 522 slice 5 (issue #1370). Before it, `method_plan` decided a
# postcondition POSITIONALLY: a later reversible crossing anywhere in the same
# method made every earlier actuation `verified-against-untrusted-read`. That
# reports that a read FOLLOWS an actuation, not that the read CHECKS it, and a
# program satisfies it with a read of something else entirely. A LIFO
# compensation run built on that verdict would report a guarantee it does not
# have, which is why this lands beside slice 3 rather than after it.
#
# What a binding can be, given what item 521 slice 4 actually landed. A
# `UiTarget` is carried BY VALUE and is not a resolved handle
# (`docs/design/565-ui-target-binding.md` §7 and §11), so revl cannot follow a
# handle from the `ui.find` that produced it to the actuation and on to the
# check. What it can do is compare how the two reads NAME the target, and that
# is what `_key` computes: an expression's provenance, with every let-bound
# name resolved through to the expression that produced it. Two `ui.find`
# calls whose provenance keys are equal name the same target the same way.
#
# That is re-resolution by name, and it is the honest shape issue #1369 names:
# it is strictly weaker than a handle, and it does NOT close the check-to-use
# race (issue #1371), because two resolutions of the same name at two instants
# may return two different controls. It is nonetheless a different and stronger
# statement than position, and the difference is measurable: a read of
# `find("Cancel")` after a click on `find("Approve")` satisfies the positional
# rule and fails this one.
#
# FRESHNESS is the second half and it is not decoration. A read that resolves
# the right target off an observation taken BEFORE the actuation checks the
# screen as it was before the step ran, which is not a postcondition at all. So
# a bound read must derive from crossings that all come after the actuation.

#: How far `_key` follows a binding chain before giving up. A cap rather than a
#: visited set because a rebound mutable name can make the chain cyclic, and an
#: opaque key is the safe answer: two opaque keys are not equal, so a chain
#: this module cannot follow never reaches the verified word.
_KEY_DEPTH = 12


def _key(expr, origin: dict, depth: int = 0):
    """The PROVENANCE KEY of an expression: what it is, with every let-bound
    name in it resolved through to the expression that produced it.

    `origin` is the bindings visible where the expression appears, so a name
    rebound later does not retroactively change an earlier key.

    Equality of keys is the question this exists to answer, and it is
    deliberately syntactic: `find(observe(region), "Approve")` and a later
    `find(observe(region), "Approve")` have the same key and name the same
    target the same way, while `find(observe(region), "Cancel")` does not. An
    expression this function cannot read reaches `("opaque", ...)`, which is
    not equal to itself across two call sites of different shape and so never
    manufactures a binding.
    """
    if not isinstance(expr, dict) or depth > _KEY_DEPTH:
        return ("opaque",)
    kind = expr.get("kind")
    if kind == "lit":
        return ("lit", repr(expr.get("value")))
    if kind == "name":
        bound = origin.get(expr.get("id"))
        if bound is None:
            # a parameter or anything else this method did not bind
            return ("free", expr.get("id"))
        return _key(bound, origin, depth + 1)
    if kind == "fn":
        return ("call", expr.get("name")) + tuple(
            _key(arg, origin, depth + 1) for arg in expr.get("args") or ())
    return ("opaque", kind, str(expr.get("kind")))


def _mentions(expr, depth: int = 0) -> set[str]:
    """Every name an expression mentions, at any depth. Used to carry the set
    of computer-use crossings a value derives from, which is what makes the
    freshness half of the binding checkable."""
    if not isinstance(expr, dict) or depth > _KEY_DEPTH:
        return set()
    if expr.get("kind") == "name":
        return {expr.get("id")}
    found: set[str] = set()
    for arg in expr.get("args") or ():
        found |= _mentions(arg, depth + 1)
    for member in ("value", "expr", "left", "right"):
        if isinstance(expr.get(member), dict):
            found |= _mentions(expr[member], depth + 1)
    return found


def _is_raised(token: str, approval_tokens) -> bool:
    """Whether the operator's authority raises `token` to `confirm-required`.

    A LADDER RUNG is raised by a rule naming its VERB. `ui.click.pixel` is a
    strictly weaker way to name the same target as `ui.click` (item 521 slice
    3), so an operator who raises `ui.click` and gets a plan reporting the
    pixel rung `unconfirmed` has had the raise escaped by a spelling in a
    different file. That is the fail-open direction, and it is the same
    argument `ui_family.taint_roles` and `ui_family.reversibility` already
    make by resolving a rung to its verb; this was the one table in the
    computer-use surface still comparing by exact string.

    The raise is MONOTONE, so resolving upward only ever raises: nothing here
    can lower a token the operator named.
    """
    tokens = approval_tokens or frozenset()
    if token in tokens:
        return True
    verb = ui_family.verb_of(token)
    return verb is not None and verb != token and verb in tokens


def method_plan(method: dict, externs: dict, approval_tokens) -> dict | None:
    """The transaction plan for one provide method, or `None` when the method
    crosses no computer-use verb.

    `externs` is `_extern_index(ir)`; `approval_tokens` is the set of
    capability tokens the operator's policy raises to `confirm-required`.
    """
    raw: list[dict] = []
    #: let-bound name -> the expression that produced it, as of the statement
    #: being walked. Rebinding a name changes what a LATER key resolves to and
    #: leaves the keys already taken alone, which is the only reading that
    #: survives a mutable local.
    origin: dict[str, dict] = {}
    #: let-bound name -> the computer-use crossings its value derives from, by
    #: step index. This is what makes freshness checkable.
    derives: dict[str, frozenset[int]] = {}
    #: let-bound name -> the step index of the `ui.find` that produced it, when
    #: one did. Slice 5 reports it; it does not act on it.
    resolver: dict[str, int] = {}
    for stmt in method.get("body") or []:
        bound = stmt.get("name") if stmt.get("step") == "let" else None
        value = stmt.get("value") if bound else None
        value_deps: frozenset[int] = frozenset()
        for mentioned in _mentions(value):
            value_deps |= derives.get(mentioned, frozenset())
        for name, approved, args in _calls(stmt):
            entry = externs.get(name)
            if entry is None:
                continue
            token = entry["token"]
            verb = ui_family.verb_of(token)
            arg_deps: frozenset[int] = frozenset()
            for arg in args:
                for mentioned in _mentions(arg):
                    arg_deps |= derives.get(mentioned, frozenset())
            if ui_family.reversibility(token) is not None:
                index = len(raw)
                targets = entry["targets"]
                target_expr = (args[targets[0]]
                               if len(targets) == 1 and len(args) > targets[0]
                               else None)
                raw.append({
                    "extern": name,
                    "token": token,
                    "compensate": entry["compensate"],
                    "inverse": entry["inverse"],
                    "approved": approved,
                    # how this crossing NAMES what it reads or acts on
                    "key": _key({"kind": "fn", "name": name, "args": args},
                                origin),
                    "targetKey": (None if target_expr is None
                                  else _key(target_expr, origin)),
                    "targetParams": len(targets),
                    "resolvedBy": (resolver.get(target_expr.get("id"))
                                   if isinstance(target_expr, dict)
                                   and target_expr.get("kind") == "name"
                                   else None),
                    "derives": arg_deps,
                })
                value_deps = value_deps | arg_deps | {index}
                if bound and verb in ui_family.TARGET_PRODUCERS:
                    resolver[bound] = index
        if bound:
            origin[bound] = value
            derives[bound] = value_deps
    if not raw:
        return None
    reversible_at = [i for i, step in enumerate(raw)
                     if ui_family.reversibility(step["token"])
                     == ui_family.REVERSIBLE]
    steps = []
    for i, step in enumerate(raw):
        token = step["token"]
        raised = _is_raised(token, approval_tokens)
        # POSITION: a later reversible crossing exists somewhere in this
        # method. It is no longer what decides the verdict; it only separates
        # `unverified` from `read-not-bound-to-this-step`.
        followed = any(j > i for j in reversible_at)
        checked_by = _checked_by(raw, i)
        steps.append({
            "extern": step["extern"],
            "token": token,
            "class": ui_family.reversibility(token),
            "effectiveClass": raised_class(token, approval_required=raised),
            "eligiblePhases": list(eligible_phases(token)),
            "residue": residue_state(token, step["compensate"]),
            "confirmation": confirmation(token, covered=step["approved"],
                                         raised=raised),
            "postcondition": postcondition(
                token, checked_by_read=checked_by is not None,
                followed_by_read=followed),
            # the read that carries this step's postcondition, by name, or
            # `None`. Naming it is what makes the verdict auditable rather
            # than a word: a reader can go and look at that read.
            "postconditionCheckedBy": (None if checked_by is None
                                       else raw[checked_by]["extern"]),
            # the `ui.find` in this method whose result this step acted on, or
            # `None` for a target that came from somewhere else. Reported, not
            # acted on: see the note on issue #1371 below.
            "targetResolvedBy": (None if step["resolvedBy"] is None
                                 else raw[step["resolvedBy"]]["extern"]),
            "targetParameters": step["targetParams"],
        })
    revert = revert_report(
        (s["extern"], s["token"], s["compensate"]) for s in raw)
    unconfirmed = [s["extern"] for s in steps
                   if s["confirmation"] == UNCONFIRMED
                   and s["residue"] == UNCOMPENSATED]
    # SLICE 3 MEETS SLICE 5. A LIFO run is triggered by an unmet
    # postcondition, so a run exists only for a step that HAS one. An actuation
    # with no bound postcondition is a step whose failure the transaction never
    # learns about, and it gets named rather than given a run it would never
    # reach.
    runs = []
    undetectable = []
    for i, step in enumerate(steps):
        if step["postcondition"] == VERIFIED_AGAINST_UNTRUSTED:
            runs.append(compensation_run(
                ((s["extern"], s["token"], s["compensate"]) for s in raw), i))
        elif step["postcondition"] in NO_POSTCONDITION:
            undetectable.append(step["extern"])
    return {
        "method": method.get("name"),
        "steps": steps,
        "revert": revert,
        # the steps this item's confirmation gate is about: they leave residue
        # no inverse describes AND no authority required a human to see them.
        # Enumerated by name so their absence from a policy is not silence.
        "unconfirmedIrreversibleSteps": unconfirmed,
        "unverifiedSteps": [s["extern"] for s in steps
                            if s["postcondition"] == UNVERIFIED],
        # slice 5's word: a read follows and checks something else
        "unboundPostconditionSteps": [s["extern"] for s in steps
                                      if s["postcondition"] == UNBOUND],
        # slice 3: the LIFO run for each step whose failure is detectable
        "compensationRuns": runs,
        # and the steps where no run would ever be triggered
        "undetectableFailureSteps": undetectable,
    }


def _checked_by(raw: list[dict], index: int) -> int | None:
    """The step index of the read that carries step `index`'s postcondition,
    or `None`.

    Three conditions, all required, and each one is a way the positional rule
    was wrong:

    1. the read comes AFTER the actuation. This is the positional rule, kept,
       because it is necessary and was only ever insufficient;
    2. the read RESOLVES A TARGET and names the same target the actuation acted
       on, by provenance key. A read of a different control checks a different
       control;
    3. every computer-use crossing the read derives from comes after the
       actuation. A read off an earlier observation reports the screen as it
       was before the step ran.

    `None` when the actuation's own target cannot be identified, which is the
    fail-closed side of `docs/design/565-ui-target-binding.md` §7's undecided
    question about which parameter is the target.
    """
    target = raw[index]["targetKey"]
    if target is None:
        return None
    for later in range(index + 1, len(raw)):
        step = raw[later]
        if ui_family.verb_of(step["token"]) not in ui_family.TARGET_PRODUCERS:
            continue
        if step["key"] != target:
            continue
        if any(source <= index for source in step["derives"]):
            continue
        return later
    return None


def plans(ir: dict, approval_tokens=frozenset()) -> list[dict]:
    """Every computer-use transaction plan in the composition, by component and
    provide method, in declaration order. `[]` for a composition that crosses
    no computer-use verb, which is every composition in the tree today."""
    externs = _extern_index(ir)
    if not externs:
        return []
    out: list[dict] = []
    for comp in ir.get("components") or []:
        # the ACTIVATION body first. It is the one place `await approval[C]`
        # can mint an `Approval[C]`, so it is the one place a computer-use
        # crossing can be `confirmed-per-crossing` today. Reading it is not a
        # completeness detail: without it this function could only ever report
        # `unconfirmed`, and a gate that can only report one value is not a
        # measurement.
        activation = {"name": "<activation>",
                      "body": [s for s in (comp.get("body") or [])
                               if isinstance(s, dict)
                               and s.get("step") != "provide"]}
        plan = method_plan(activation, externs, approval_tokens)
        if plan is not None:
            plan["component"] = comp.get("name")
            plan["key"] = "<activation>"
            out.append(plan)
        for entry in comp.get("body") or []:
            if not isinstance(entry, dict) or entry.get("step") != "provide":
                continue
            for method in entry.get("methods") or []:
                plan = method_plan(method, externs, approval_tokens)
                if plan is None:
                    continue
                plan["component"] = comp.get("name")
                plan["key"] = entry.get("name")
                out.append(plan)
    return out

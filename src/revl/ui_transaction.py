"""UI transaction phases, the revert split, and the confirmation raise
(roadmap item 522, issue #1196, `docs/design/553-ui-transaction-phases.md`,
Slice 2).

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

WHAT THIS MODULE DOES NOT DO. It runs nothing. There is no runtime transaction,
no LIFO execution, and no phase scheduler; the phases below are a COMPILE-TIME
ELIGIBILITY decision (which phases a step of a given class may occupy) and a
static plan read off the lowered IR. The substrate that would execute them is
roadmap item 539, upstream `inso1337/revl-harness#11`. Saying so here is the
point of section 3 of the slice-1 design: an implied guarantee revl does not
enforce is worse than no phase list at all.
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

#: A read pair follows the actuation. The strongest word this module prints.
VERIFIED_AGAINST_UNTRUSTED = "verified-against-untrusted-read"
#: The actuation is followed by no read that could bear a postcondition.
UNVERIFIED = "unverified"
#: A read commits nothing, so there is no postcondition to hold it to.
NOT_APPLICABLE = "not-applicable"

POSTCONDITION_MEANING: dict[str, str] = {
    VERIFIED_AGAINST_UNTRUSTED:
        "a fresh `screen.observe` / `ui.find` read follows this actuation and "
        "can carry its postcondition. The read came from the application under "
        "test, so it RAISES CONFIDENCE and does not establish a fact - there is "
        "deliberately no plain `verified` in this vocabulary "
        "(docs/design/538-ui-transactions.md §4)",
    UNVERIFIED:
        "this actuation is followed by no read. Its return value says it was "
        "delivered, which is not the same claim as the business effect having "
        "occurred (docs/design/538-ui-transactions.md §3.1)",
    NOT_APPLICABLE:
        "a read actuates nothing, so it has no postcondition of its own",
}


def postcondition(token: str, *, followed_by_read: bool) -> str | None:
    """The postcondition state of one crossing, or `None` for a non-UI token.

    Note what is NOT here: there is no argument by which this function returns
    plain `verified`. A postcondition read through a surface the target
    application controls cannot be stronger than that surface."""
    cls = ui_family.reversibility(token)
    if cls is None:
        return None
    if cls == ui_family.REVERSIBLE:
        return NOT_APPLICABLE
    return VERIFIED_AGAINST_UNTRUSTED if followed_by_read else UNVERIFIED


# ------------------------------------------------- the static plan over IR
#
# The phase list, read off a lowered composition. One plan per provide method
# that crosses at least one computer-use verb: the ordered steps, each step's
# class, eligible phases, residue state, confirmation state and postcondition
# state, plus the LIFO compensation order and the weakest-part aggregate.
#
# This is a READING, not an execution. It runs nothing and schedules nothing.


def _extern_index(ir: dict) -> dict:
    """extern name -> (capability token, has_compensate) for every emission
    extern in the composition. A token per extern; the first declared
    capability is the one the G8 audit surface names for it."""
    index: dict[str, tuple[str, bool]] = {}
    for entry in ir.get("externs") or []:
        if entry.get("class") != "emission":
            continue
        caps = entry.get("capabilities") or []
        token = caps[0] if caps else entry.get("name")
        index[entry.get("name")] = (token, entry.get("compensate") is not None)
    return index


def _calls(node) -> list[tuple[str, bool]]:
    """Every `(extern name, carries an approval edge)` a statement calls, in
    source order. Walks `let`, `emit` and plain expression statements; an
    `emit ... with a` records its edge so `confirmation` can read it."""
    found: list[tuple[str, bool]] = []
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
        found.append((expr["name"], approved))
    return found


def method_plan(method: dict, externs: dict, approval_tokens) -> dict | None:
    """The transaction plan for one provide method, or `None` when the method
    crosses no computer-use verb.

    `externs` is `_extern_index(ir)`; `approval_tokens` is the set of
    capability tokens the operator's policy raises to `confirm-required`.
    """
    raw: list[dict] = []
    for stmt in method.get("body") or []:
        for name, approved in _calls(stmt):
            entry = externs.get(name)
            if entry is None:
                continue
            token, has_compensate = entry
            if ui_family.reversibility(token) is None:
                continue
            raw.append({"extern": name, "token": token,
                        "compensate": has_compensate, "approved": approved})
    if not raw:
        return None
    # a postcondition read is a LATER reversible crossing in the same method.
    # Read positionally and honestly: this says a read follows the actuation,
    # not that the read checks THAT actuation. Section 5 of the design note
    # says so in the report's own words rather than leaving it to be assumed.
    reversible_at = [i for i, step in enumerate(raw)
                     if ui_family.reversibility(step["token"])
                     == ui_family.REVERSIBLE]
    steps = []
    for i, step in enumerate(raw):
        token = step["token"]
        raised = token in (approval_tokens or frozenset())
        followed = any(j > i for j in reversible_at)
        steps.append({
            "extern": step["extern"],
            "token": token,
            "class": ui_family.reversibility(token),
            "effectiveClass": raised_class(token, approval_required=raised),
            "eligiblePhases": list(eligible_phases(token)),
            "residue": residue_state(token, step["compensate"]),
            "confirmation": confirmation(token, covered=step["approved"],
                                         raised=raised),
            "postcondition": postcondition(token, followed_by_read=followed),
        })
    revert = revert_report(
        (s["extern"], s["token"], s["compensate"]) for s in raw)
    unconfirmed = [s["extern"] for s in steps
                   if s["confirmation"] == UNCONFIRMED
                   and s["residue"] == UNCOMPENSATED]
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
    }


def plans(ir: dict, approval_tokens=frozenset()) -> list[dict]:
    """Every computer-use transaction plan in the composition, by component and
    provide method, in declaration order. `[]` for a composition that crosses
    no computer-use verb, which is every composition in the tree today."""
    externs = _extern_index(ir)
    if not externs:
        return []
    out: list[dict] = []
    for comp in ir.get("components") or []:
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

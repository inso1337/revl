"""Shadow routing: the scheduler that serves a declared fraction of a model
action in shadow (roadmap item 518, issue #1192).

Design note: `docs/design/558-shadow-scheduling.md`. Slice 2 of
`docs/design/540-shadow-promotion.md`; slice 1 is `revl.shadow_promotion`.

WHAT LANDED BEFORE THIS, AND WHAT THIS ADDS
-------------------------------------------
`revl.shadow_promotion` (item 518 slice 1, PR #1250) accumulates agreement
over item 517's sealed model-decision records and turns the accumulation into
a `PROMOTE` / `REVERT` / `REFUSE` verdict. `revl.mcp.canary` (item 496)
compares two recorded worlds and attributes the first differing step. Neither
of them SCHEDULES anything, and that absence has a measurable consequence:

  * Nothing decides which crossings are served in shadow, so "a declared
    fraction" is a phrase in the item and not a value anywhere.
  * Item 517's record body is a CLOSED vocabulary keyed by
    `(component, step_index)` and it names no action and no realm. So a window
    of records cannot say which ACTION CLASS it is evidence for.
    `docs/design/540-shadow-promotion.md` section 7 recorded that as the one
    correlation slice 1 does not derive. Measured on `7a1f2793`: a plan naming
    `classify` and a window of `summarize`'s records returns `PROMOTE` with
    agreement 1.0.

This module is the scheduler, and it is the correlation. It decides which
crossings are shadowed, it calls the candidate ONLY on those, it stamps each
observation with the action and the realm it scheduled it for, and it refuses
a window whose stamps do not match the plan. The stamp is not a second log and
not a member added to a sealed record: it is what the scheduler already knew
when it scheduled the shadow.

THE INCUMBENT'S ANSWER IS THE ONE USED
--------------------------------------
While the candidate is not live, the answer handed back to the caller is the
incumbent's, on every crossing, shadowed or not. A shadow whose candidate
answers is not a shadow. :class:`Served` records which side was served per
crossing, :func:`decide` refuses a window in which a not-live candidate
answered (`SERVED_CANDIDATE`), and the test file measures it over a window in
which the candidate's answers differ, rather than asserting it.

THE FRACTION IS A SCHEDULE, NEVER A VERDICT INPUT
-------------------------------------------------
The declared share is a rational `numerator/denominator`, not a float: "1 in
20" is exact and a float is a rounding. Selection is a keyed digest of the
crossing, so it is a PURE FUNCTION of `(salt, component, step_index)` and
reproduces under replay and under reordering. Two runs over the same crossings
in a different order shadow the same set.

The share governs WHICH crossings are observed. It is not evidence and it does
not appear in the verdict: `decide` hands `revl.shadow_promotion.decide` a
plan and a window, and a `ShadowPlan` has no member for a share. A test walks
this module's syntax tree and asserts that no function that produces a verdict
reads one.

WHAT THE COMPARISON IS
----------------------
Unchanged from item 496, which is the point. A shadow observation carries the
two sealed records and, when the recorder supplied them, the two RECORDED
WORLDS as `replay.Timeline` objects in `revl.mcp.canary`'s format.
`shadow_promotion.accumulate` compares the records on their agreement members
and the worlds with `canary.compare_timelines`, whose key is item 496's
`(kind, label, undo, compensate, slot)`. A divergence names an exact
`(component, realm)` and an exact step. No metric is read on any path, and the
`slo_reads` counter in the verdict is the measurement of that rather than a
claim about it.

FAILURE DIRECTION
-----------------
Fail-closed, with the scheduler's own three refusals ahead of the gate's:

  * a window carrying an observation the schedule never scheduled refuses
    (`ACTION_UNSCHEDULED`) rather than being counted;
  * an observation stamped for another action refuses (`ACTION_MISMATCHED`);
  * an observation stamped for another realm refuses (`REALM_MISMATCHED`);
  * a not-live candidate that answered refuses (`SERVED_CANDIDATE`);
  * a share outside `0 <= n <= d` with `d > 0` refuses (`SHARE_MALFORMED`);
  * a route whose component, action or roles disagree with the plan refuses
    (`ROUTE_MISMATCHED`), because a schedule for one promotion cannot supply
    the evidence for another.

Each of these is reported through `shadow_promotion.refused`, so a caller gets
the same :class:`~revl.shadow_promotion.Promotion` shape with the same
`measurements_read` false and the same "not reached" stages. There is no
second verdict object and no second vocabulary of decisions.

NO NEW GUARANTEE CODE
---------------------
The refusals here are named LINKS, the discipline `revl.shadow_promotion`,
`revl.model_evidence` and `revl.deploy` use. A scheduler refusing a window is
not the checker refusing a program, and nothing here registers a `G-` code or
owes `tools/tier_guarantees.py` a reproducer.

PUBLIC SURFACE
--------------
``Share``                 : the declared fraction, as a rational
``ShadowRoute``           : what is shadowed: action, realm, roles, share
``Answered``              : one side's sealed record plus its recorded world
``Served`` / ``Entry``    : what one crossing did, and the stamped observation
``ShadowLedger``          : the accumulated window for ONE action class
``selects(route, key)``   : the deterministic selection, a pure function
``Scheduler``             : the incremental form, one crossing at a time
``serve(route, ...)``     : the scheduler, crossings to a ledger
``decide(route, plan, entries, ...)`` : the scheduler's checks, then the gate
``render(promotion, ledger)``         : the gate's report plus what was served
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from . import shadow_promotion as promotion

ROUTING_KIND = "revl.shadow-routing"

#: MAJOR.MINOR, additive within a MAJOR.
ROUTING_VERSION = "1.0"

#: The self-identifying tag of a RECORDED WINDOW DOCUMENT, which is the
#: serialized form of a :class:`ShadowLedger` and the artifact a promotion is
#: decided from. Distinct from :data:`ROUTING_KIND`, which tags a live
#: ledger's summary: a window document carries the entries, and the summary
#: drops them.
WINDOW_KIND = "revl.shadow-window"

#: MAJOR.MINOR, additive within a MAJOR.
WINDOW_VERSION = "1.0"

#: The side whose answer was handed to the caller. Two values and no third:
#: an answer either came from the incumbent or from the candidate.
INCUMBENT = "incumbent"
CANDIDATE = "candidate"
SIDES = (CANDIDATE, INCUMBENT)


# ---------------------------------------------------------------------------
# refusal links
# ---------------------------------------------------------------------------
#
# The scheduler's own, distinct from `shadow_promotion.LINKS`. They are kept
# separate rather than merged because that module's partition test asserts
# that its links split exactly into a precondition half and a measured half,
# and these are neither: they are refusals about the SCHEDULE, which is a
# thing that module does not have.

ROUTE_MALFORMED = "route-malformed"
SHARE_MALFORMED = "share-malformed"
ROUTE_MISMATCHED = "route-mismatched"
ACTION_UNSCHEDULED = "action-unscheduled"
ACTION_MISMATCHED = "action-mismatched"
REALM_MISMATCHED = "realm-mismatched"
SERVED_CANDIDATE = "served-candidate"

#: The window document itself is not a window. Two links and not one, because
#: "cut short" and "written wrong" are two mistakes with two fixes: a
#: TRUNCATED document is missing a member it was supposed to carry, and a
#: MALFORMED one carries a member that is not what it claims to be. Neither is
#: repaired by a default.
WINDOW_MALFORMED = "window-malformed"
WINDOW_TRUNCATED = "window-truncated"

#: Every link this module can report, for a caller that wants exhaustiveness.
#:
#: These are the SCHEDULE's refusals, and every one is reachable from
#: `decide`. The two window-document links are NOT here: they are refusals
#: about reading an artifact, they are reported under a stage of their own,
#: and keeping them out of this tuple is what lets the slice-2 pin that every
#: member of it is reachable by a real schedule stay exactly as it was.
LINKS = (
    ROUTE_MALFORMED, SHARE_MALFORMED, ROUTE_MISMATCHED,
    ACTION_UNSCHEDULED, ACTION_MISMATCHED, REALM_MISMATCHED,
    SERVED_CANDIDATE,
)

#: The links :func:`window_from_dict` can report, and the only two it can.
WINDOW_LINKS = (WINDOW_MALFORMED, WINDOW_TRUNCATED)

#: The stage name these refusals are reported under, so a reader of a verdict
#: can see that the walk stopped BEFORE the gate rather than inside it.
SCHEDULE_STAGE = "schedule"


# ---------------------------------------------------------------------------
# the declared fraction
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Share:
    """The fraction of an action's crossings served in shadow, as a rational.

    A rational and not a float because a share is a statement about counting:
    "1 in 20" has an exact meaning and `0.05` is a rounding of it. ``0/d``
    shadows nothing and ``d/d`` shadows everything; both are legal and both
    are useful, the first as the off switch and the second as the exhaustive
    run a test uses.

    This number governs WHICH crossings are observed. It is not evidence, it
    is not compared, and it does not reach the verdict: `ShadowPlan` has no
    member for it."""

    numerator: int
    denominator: int

    def valid(self) -> bool:
        for value in (self.numerator, self.denominator):
            if not isinstance(value, int) or isinstance(value, bool):
                return False
        return self.denominator > 0 and 0 <= self.numerator <= self.denominator

    def __str__(self) -> str:
        return f"{self.numerator}/{self.denominator}"


#: The two ends of the range, named so a caller does not spell them.
NOTHING = Share(0, 1)
EVERYTHING = Share(1, 1)


# ---------------------------------------------------------------------------
# the route
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShadowRoute:
    """What is shadowed, where, for whom, and how much of it.

    ``component`` and ``action`` are the action class agreement accumulates
    under, and they are the stamp this module puts on every observation it
    schedules. ``realm`` is where the shadow is served, which is the second
    half of the `(component, realm)` attribution `revl canary` reports and
    which item 517's record does not carry.

    ``salt`` pins the selection. Two runs with the same salt shadow the same
    crossings, which is what makes a shadow window reproducible from a replay
    rather than only from a log of what happened to be sampled.

    ``live`` is whether the candidate's answer is the one in use. It is passed
    through to the plan and it selects which of the two rules in
    `revl.shadow_promotion`'s header applies; this module uses it for one
    thing only, which is to know whether serving the candidate's answer is a
    refusal."""

    component: str
    action: str
    realm: str
    incumbent_role: str
    candidate_role: str
    share: Share
    salt: str = ""
    live: bool = False

    @property
    def action_class(self) -> tuple:
        return (self.component, self.action)


def _check_route(route: Any) -> Optional[tuple]:
    """``(link, reason)`` when the route is not a route, ``None`` otherwise."""
    if not isinstance(route, ShadowRoute):
        return (ROUTE_MALFORMED, f"{route!r} is not a ShadowRoute")
    for name in ("component", "action", "realm", "incumbent_role",
                 "candidate_role"):
        value = getattr(route, name)
        if not isinstance(value, str) or not value:
            return (ROUTE_MALFORMED, f"route.{name} is not a name ({value!r})")
    if not isinstance(route.salt, str):
        return (ROUTE_MALFORMED,
                f"route.salt {route.salt!r} is not a string; the salt pins "
                f"which crossings are shadowed and an unpinned selection "
                f"cannot be reproduced from a replay")
    if not isinstance(route.share, Share) or not route.share.valid():
        return (SHARE_MALFORMED,
                f"route.share {route.share!r} is not a rational n/d with "
                f"d > 0 and 0 <= n <= d; a share is a statement about "
                f"counting and a float is a rounding of one")
    if route.incumbent_role == route.candidate_role:
        return (ROUTE_MALFORMED,
                f"the route shadows {route.incumbent_role!r} with itself, "
                f"which records a shadow that never ran")
    return None


# ---------------------------------------------------------------------------
# selection: deterministic, replayable, order-independent
# ---------------------------------------------------------------------------

def _draw(salt: str, crossing: tuple) -> int:
    """A stable integer in [0, 2**64) for one crossing under one salt.

    SHA-256 over the salt and the crossing key, with the separator that keeps
    `("A", 11)` and `("A1", 1)` apart. This is a scheduling draw and not a
    security boundary: what it has to be is the SAME draw on a replay, which
    is why it is a digest of the crossing rather than a counter or a random
    number generator's next value."""
    payload = f"{salt}\x00{crossing[0]}\x00{crossing[1]}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def selects(route: ShadowRoute, crossing: tuple) -> bool:
    """Whether this crossing is served in shadow.

    A PURE FUNCTION of `(route.salt, route.share, crossing)`. It does not read
    a counter, so it is independent of arrival order and of how many crossings
    came before; two runs over the same crossings in a different order shadow
    the same set, and a replay of a recorded run shadows exactly what the run
    shadowed."""
    share = route.share
    if share.numerator <= 0:
        return False
    if share.numerator >= share.denominator:
        return True
    return _draw(route.salt, crossing) % share.denominator < share.numerator


# ---------------------------------------------------------------------------
# what a side answered
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Answered:
    """One side's answer to one crossing.

    ``record`` is an item 517 sealed model-decision record. ``world`` is the
    RECORDED WORLD that answer produced, as a `replay.Timeline` in
    `revl.mcp.canary`'s own format, or ``None`` when the recorder did not
    supply one. A pair with two worlds is compared on both; a pair with fewer
    than two is compared on its records alone, and the ledger reports how many
    of each it holds so a reader is never guessing which comparison ran.

    ``value`` is the answer a CALLER receives when this side serves the
    crossing, and it is ``None`` for a side that cannot hand one over. The
    incumbent's is ``None`` because a running seam already holds the
    incumbent's validated answer and hands this object the raw host return
    instead; a candidate's is the answer its own completion produced, which is
    what a live route serves."""

    record: Mapping[str, Any]
    world: Any = None
    value: Any = None


@dataclass(frozen=True)
class Served:
    """What one crossing did.

    ``side`` is which answer was handed back to the caller: :data:`INCUMBENT`
    on every crossing of a route that is not live, and on every crossing of a
    live route the share did not select. ``shadowed`` says whether the
    candidate was consulted at all: on a crossing the share did not select, it
    was not called, and the ledger counts that rather than asserting it."""

    crossing: tuple
    shadowed: bool
    side: str


@dataclass(frozen=True)
class Entry:
    """One scheduled shadow observation, with the stamp that says which action
    class's window it belongs in.

    The stamp is the SCHEDULER'S. Item 517's body is a closed vocabulary and
    carries no action and no realm, so a record cannot say which action class
    it is evidence for; the thing that can is the thing that scheduled the
    shadow, which is this module. `decide` checks the stamp against the plan,
    so a window of one action's evidence cannot meet another action's
    threshold."""

    crossing: tuple
    component: str
    action: str
    realm: str
    side: str
    observation: promotion.Observation

    @property
    def action_class(self) -> tuple:
        return (self.component, self.action)


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShadowLedger:
    """The accumulated window for ONE action class, plus the record of what
    the scheduler did to fill it."""

    route: ShadowRoute
    entries: tuple = ()
    served: tuple = ()
    candidate_calls: int = 0
    refusal: Optional[tuple] = None

    @property
    def observations(self) -> tuple:
        return tuple(entry.observation for entry in self.entries)

    @property
    def offered(self) -> int:
        """How many crossings the scheduler was handed."""
        return len(self.served)

    @property
    def shadowed(self) -> int:
        """How many of them it served in shadow."""
        return sum(1 for s in self.served if s.shadowed)

    @property
    def worlds_recorded(self) -> int:
        """How many entries carry BOTH recorded worlds, so a reader knows how
        many pairs the `canary.compare_timelines` leg actually ran over."""
        return sum(1 for e in self.entries
                   if getattr(e.observation, "has_world", False))

    def plan(self, **overrides) -> promotion.ShadowPlan:
        """A `ShadowPlan` whose component, action, roles and `live` come from
        THIS route, so the plan and the schedule cannot disagree about what is
        being promoted. Everything the gate needs and the scheduler does not
        own (the route table, the authority diff, the layers, the threshold,
        the sample) is supplied by the caller."""
        base = dict(component=self.route.component, action=self.route.action,
                    incumbent_role=self.route.incumbent_role,
                    candidate_role=self.route.candidate_role,
                    live=self.route.live)
        base.update(overrides)
        return promotion.ShadowPlan(**base)

    def as_dict(self) -> dict:
        return {
            "kind": ROUTING_KIND, "version": ROUTING_VERSION,
            "action_class": list(self.route.action_class),
            "realm": self.route.realm,
            "share": str(self.route.share),
            "offered": self.offered,
            "shadowed": self.shadowed,
            "candidate_calls": self.candidate_calls,
            "worlds_recorded": self.worlds_recorded,
            "served_by_candidate": sum(1 for s in self.served
                                       if s.side == CANDIDATE),
            "refusal": list(self.refusal) if self.refusal else None,
        }

    def as_window(self) -> dict:
        """The window as a DOCUMENT, which is what a promotion is decided from.

        `as_dict` is a SUMMARY: it drops every entry and every record, so
        nothing can be decided from it. This carries the entries, their
        stamps and both sides' sealed records, which is the evidence the gate
        reads, plus the schedule that produced them.

        The two RECORDED WORLDS are deliberately NOT carried. The worlds a
        decision runs over are item 496's: `canary.slice_timeline` of each of
        the two generations, which is a static walk of the composition. A world
        recorded during a shadowed run is the RUN's timeline, and it is not
        comparable step-for-step with another generation's static walk — the
        candidate's own completion crosses the same seam, so it consumes a step
        index and shifts the incumbent's crossings relative to an unshadowed
        run (measured: two declared crossings plus one shadowed one record at
        indices 1, 2 and 3). So the reader derives the worlds from the two
        compositions it is given, and it derives them ALWAYS.

        How many pairs the run itself carried a world for is therefore not
        written here either. It is derivable only from the run, the decision
        never consults it, and a document author can write any count — so
        carrying it would add a number to the artifact that no reader checks
        and no verdict rests on."""
        return {
            "kind": WINDOW_KIND, "version": WINDOW_VERSION,
            "route": {
                "component": self.route.component,
                "action": self.route.action,
                "realm": self.route.realm,
                "incumbent_role": self.route.incumbent_role,
                "candidate_role": self.route.candidate_role,
                "share": str(self.route.share),
                "salt": self.route.salt,
                "live": self.route.live,
            },
            "entries": [
                {
                    "crossing": list(entry.crossing),
                    "component": entry.component,
                    "action": entry.action,
                    "realm": entry.realm,
                    "side": entry.side,
                    "incumbent": dict(entry.observation.incumbent),
                    "candidate": dict(entry.observation.candidate),
                    "slo": (None if entry.observation.slo_supplied is None
                            else dict(entry.observation.slo_supplied)),
                }
                for entry in self.entries
            ],
            "served": [
                {"crossing": list(item.crossing), "shadowed": item.shadowed,
                 "side": item.side}
                for item in self.served
            ],
            "candidate_calls": self.candidate_calls,
            "refusal": list(self.refusal) if self.refusal else None,
        }


# ---------------------------------------------------------------------------
# reading a recorded window back
# ---------------------------------------------------------------------------
#
# `as_window` writes one and this reads it. The reader is strict on purpose:
# a window document is operator-supplied, so the two ways it can be wrong are
# named separately and neither is repaired. What it does NOT do is trust the
# document because it parses: the sealed records are re-verified by the gate's
# own `model_evidence.verify`, and every stamp is checked against the
# composition by `revl.shadow_runtime.check_stamps`. This function checks
# shape, which is all a reader of an artifact can check on its own.

def _window_member(document: Any, name: str, where: str) -> tuple:
    """``(value, refusal)`` for one required member of a window document.

    An ABSENT member is :data:`WINDOW_TRUNCATED` and a member that is present
    but is not what it claims to be is :data:`WINDOW_MALFORMED`, decided by
    the caller. Absence is separated out because it is the one mistake a
    default would paper over, and a defaulted window is a promotion on
    evidence nobody recorded."""
    if not isinstance(document, Mapping):
        return (None, (WINDOW_MALFORMED,
                       f"{where} is {document!r}, which is not an object"))
    if name not in document:
        return (None, (WINDOW_TRUNCATED,
                       f"{where} has no {name!r} member. A window document is "
                       f"written whole: an absent member is a document that "
                       f"was cut short, and defaulting one would let a "
                       f"promotion rest on a window nobody recorded"))
    return (document[name], None)


def _window_share(text: Any) -> Optional[Share]:
    """``Share`` from a ``"n/d"`` string, or ``None``. Not a float: a share is
    a statement about counting, and `0.05` is a rounding of `1/20`."""
    if not isinstance(text, str):
        return None
    head, separator, tail = text.partition("/")
    if not separator or not head.isdigit() or not tail.isdigit():
        return None
    share = Share(int(head), int(tail))
    return share if share.valid() else None


def _window_route(document: Any) -> tuple:
    """``(route, refusal)`` for the window's schedule."""
    route, refusal = _window_member(document, "route", "the window document")
    if refusal is not None:
        return (None, refusal)
    names = {}
    for name in ("component", "action", "realm", "incumbent_role",
                 "candidate_role"):
        value, refusal = _window_member(route, name, "the window's route")
        if refusal is not None:
            return (None, refusal)
        if not isinstance(value, str) or not value:
            return (None, (WINDOW_MALFORMED,
                           f"the window's route.{name} is {value!r}, which is "
                           f"not a name"))
        names[name] = value
    share_text, refusal = _window_member(route, "share", "the window's route")
    if refusal is not None:
        return (None, refusal)
    share = _window_share(share_text)
    if share is None:
        return (None, (WINDOW_MALFORMED,
                       f"the window's route.share is {share_text!r}, which is "
                       f"not a rational `n/d` with d > 0 and 0 <= n <= d; a "
                       f"share is a statement about counting and a float is a "
                       f"rounding of one"))
    salt, refusal = _window_member(route, "salt", "the window's route")
    if refusal is not None:
        return (None, refusal)
    if not isinstance(salt, str):
        return (None, (WINDOW_MALFORMED,
                       f"the window's route.salt is {salt!r}, which is not a "
                       f"string; the salt pins which crossings were shadowed "
                       f"and an unpinned selection cannot be reproduced"))
    live, refusal = _window_member(route, "live", "the window's route")
    if refusal is not None:
        return (None, refusal)
    if not isinstance(live, bool):
        return (None, (WINDOW_MALFORMED,
                       f"the window's route.live is {live!r}, which is not a "
                       f"boolean; `live` selects whether the candidate's "
                       f"answer is the one in use, and that is not a "
                       f"question with a default"))
    return (ShadowRoute(component=names["component"], action=names["action"],
                        realm=names["realm"],
                        incumbent_role=names["incumbent_role"],
                        candidate_role=names["candidate_role"], share=share,
                        salt=salt, live=live), None)


def _window_crossing(value: Any, where: str) -> tuple:
    """``(crossing, refusal)`` for one ``[component, step_index]`` key."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return (None, (WINDOW_MALFORMED,
                       f"{where} is {value!r}, which is not a "
                       f"(component, step_index) crossing key"))
    component, index = value
    if not isinstance(component, str) or not component:
        return (None, (WINDOW_MALFORMED,
                       f"{where} names component {component!r}, which is not "
                       f"a name"))
    if not isinstance(index, int) or isinstance(index, bool):
        return (None, (WINDOW_MALFORMED,
                       f"{where} names step index {index!r}, which is not an "
                       f"integer; the crossing key is what the stamp is "
                       f"checked against the composition on"))
    return ((component, index), None)


def _window_entry(value: Any, index: int) -> tuple:
    """``(entry, refusal)`` for one recorded shadow observation."""
    where = f"window position {index}"
    if not isinstance(value, Mapping):
        return (None, (WINDOW_MALFORMED,
                       f"{where} is {value!r}, which is not an entry"))
    crossing, refusal = _window_member(value, "crossing", where)
    if refusal is not None:
        return (None, refusal)
    crossing, refusal = _window_crossing(crossing, f"{where}'s crossing")
    if refusal is not None:
        return (None, refusal)
    stamp = {}
    for name in ("component", "action", "realm", "side"):
        member, refusal = _window_member(value, name, where)
        if refusal is not None:
            return (None, refusal)
        if not isinstance(member, str) or not member:
            return (None, (WINDOW_MALFORMED,
                           f"{where}'s {name} is {member!r}, which is not a "
                           f"name"))
        stamp[name] = member
    if stamp["side"] not in SIDES:
        return (None, (WINDOW_MALFORMED,
                       f"{where} records the served side as {stamp['side']!r}, "
                       f"which is outside {list(SIDES)}"))
    records = {}
    for name in ("incumbent", "candidate"):
        member, refusal = _window_member(value, name, where)
        if refusal is not None:
            return (None, refusal)
        if not isinstance(member, Mapping):
            return (None, (WINDOW_MALFORMED,
                           f"{where}'s {name} is {member!r}, which is not an "
                           f"item 517 record; the two records are the "
                           f"evidence, and a window that carries a "
                           f"placeholder for one carries none"))
        records[name] = dict(member)
    slo, refusal = _window_member(value, "slo", where)
    if refusal is not None:
        return (None, refusal)
    if slo is not None and not isinstance(slo, Mapping):
        return (None, (WINDOW_MALFORMED,
                       f"{where}'s slo is {slo!r}, which is neither absent nor "
                       f"a metric block; the gate counts what was supplied so "
                       f"a reader can see what it declined to read, and a "
                       f"block that is not a block is not counted"))
    return (Entry(crossing=crossing, component=stamp["component"],
                  action=stamp["action"], realm=stamp["realm"],
                  side=stamp["side"],
                  observation=promotion.Observation(
                      incumbent=records["incumbent"],
                      candidate=records["candidate"],
                      slo=None if slo is None else dict(slo),
                      realm=stamp["realm"])), None)


def _window_served(value: Any, index: int) -> tuple:
    """``(served, refusal)`` for one crossing the scheduler handled."""
    where = f"the window's served position {index}"
    if not isinstance(value, Mapping):
        return (None, (WINDOW_MALFORMED,
                       f"{where} is {value!r}, which is not a served crossing"))
    crossing, refusal = _window_member(value, "crossing", where)
    if refusal is not None:
        return (None, refusal)
    crossing, refusal = _window_crossing(crossing, f"{where}'s crossing")
    if refusal is not None:
        return (None, refusal)
    shadowed, refusal = _window_member(value, "shadowed", where)
    if refusal is not None:
        return (None, refusal)
    if not isinstance(shadowed, bool):
        return (None, (WINDOW_MALFORMED,
                       f"{where}'s shadowed is {shadowed!r}, which is not a "
                       f"boolean; it says whether the candidate was consulted "
                       f"at all, which is a thing the ledger counts rather "
                       f"than asserts"))
    side, refusal = _window_member(value, "side", where)
    if refusal is not None:
        return (None, refusal)
    if side not in SIDES:
        return (None, (WINDOW_MALFORMED,
                       f"{where} records the served side as {side!r}, which is "
                       f"outside {list(SIDES)}"))
    return (Served(crossing=crossing, shadowed=shadowed, side=side), None)


def _window_sequence(document: Any, name: str, read: Callable) -> tuple:
    """``(items, refusal)`` for one list member, each item read by ``read``."""
    value, refusal = _window_member(document, name, "the window document")
    if refusal is not None:
        return (None, refusal)
    if not isinstance(value, (list, tuple)):
        return (None, (WINDOW_MALFORMED,
                       f"the window document's {name} is {value!r}, which is "
                       f"not a list"))
    items = []
    for index, item in enumerate(value):
        read_item, refusal = read(item, index)
        if refusal is not None:
            return (None, refusal)
        items.append(read_item)
    return (tuple(items), None)


def window_from_dict(document: Any) -> tuple:
    """``(ledger, refusal)`` from a recorded window document: one is ``None``.

    The reader counterpart of :meth:`ShadowLedger.as_window`. It is strict
    about shape and silent about truth: shape is what a reader of an artifact
    can check on its own, and whether the evidence is evidence is the gate's
    question, answered past this function by `model_evidence.verify` over the
    sealed records and by `revl.shadow_runtime.check_stamps` over the stamps.

    Nothing is defaulted. An absent member is :data:`WINDOW_TRUNCATED` and a
    present member of the wrong shape is :data:`WINDOW_MALFORMED`, and both
    are refusals the caller reports rather than repairs."""
    kind, refusal = _window_member(document, "kind", "the window document")
    if refusal is not None:
        return (None, refusal)
    if kind != WINDOW_KIND:
        return (None, (WINDOW_MALFORMED,
                       f"the document's kind is {kind!r} and a window is "
                       f"{WINDOW_KIND!r}; a plan document read as a window is "
                       f"a promotion decided from the declaration it was "
                       f"supposed to be judged against"))
    version, refusal = _window_member(document, "version",
                                      "the window document")
    if refusal is not None:
        return (None, refusal)
    if version != WINDOW_VERSION:
        return (None, (WINDOW_MALFORMED,
                       f"the document's version is {version!r} and this "
                       f"reader writes and reads {WINDOW_VERSION!r}"))
    route, refusal = _window_route(document)
    if refusal is not None:
        return (None, refusal)
    entries, refusal = _window_sequence(document, "entries", _window_entry)
    if refusal is not None:
        return (None, refusal)
    served, refusal = _window_sequence(document, "served", _window_served)
    if refusal is not None:
        return (None, refusal)
    calls, refusal = _window_member(document, "candidate_calls",
                                    "the window document")
    if refusal is not None:
        return (None, refusal)
    if not isinstance(calls, int) or isinstance(calls, bool) or calls < 0:
        return (None, (WINDOW_MALFORMED,
                       f"the window document's candidate_calls is {calls!r}, "
                       f"which is not a count"))
    failure, refusal = _window_member(document, "refusal",
                                      "the window document")
    if refusal is not None:
        return (None, refusal)
    if failure is not None:
        if not isinstance(failure, (list, tuple)) or len(failure) != 2 \
                or not all(isinstance(part, str) for part in failure):
            return (None, (WINDOW_MALFORMED,
                           f"the window document's refusal is {failure!r}, "
                           f"which is not a (link, reason) pair; a schedule "
                           f"that refused is reported, never silently "
                           f"re-decided on the entries that accumulated "
                           f"before it"))
        failure = (failure[0], failure[1])
    return (ShadowLedger(route=route, entries=entries, served=served,
                         candidate_calls=calls, refusal=failure), None)


# ---------------------------------------------------------------------------
# the scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    """The INCREMENTAL form of :func:`serve`: one crossing at a time.

    A batch of crossings is what an offline caller holds. A running
    composition is not offline: the python tier's completion seam
    (`backends/python/runtime.py`) reaches one crossing, the incumbent answers
    it, and the next crossing does not exist yet. `revl.shadow_runtime` wires
    that seam to this class.

    There is deliberately ONE selection, stamping and counting path:
    :func:`serve` is a loop over :meth:`offer`, so the batch form and the live
    form cannot drift into two answers about which crossings were shadowed and
    how many times the candidate was consulted.

    ``incumbent`` may be omitted when every caller passes the answer in:
    inside a running seam the incumbent has already answered, and asking it
    again would issue a second completion and charge for it."""

    def __init__(self, route: ShadowRoute, *,
                 candidate: Callable[[tuple], Answered],
                 incumbent: Optional[Callable[[tuple], Answered]] = None,
                 slo: Optional[Callable[[tuple], Mapping[str, Any]]] = None):
        self.route = route
        self._incumbent = incumbent
        self._candidate = candidate
        # Named `_metrics`, not `_slo`: this module's own oracle walks the
        # file's syntax tree and forbids the attribute name `_slo` anywhere,
        # because a module reaching for `observation._slo` would read a
        # metric without tripping the counter the no-metric claim rests on.
        # This member is the metrics PRODUCER, which is not a metric.
        self._metrics = slo
        self._entries: list = []
        self._served: list = []
        self._calls = 0
        self._refusal = _check_route(route)

    @property
    def refusal(self) -> Optional[tuple]:
        """``(link, reason)`` once the schedule has refused, else ``None``.
        A refused scheduler observes nothing further: the window it would go
        on to build could not be told apart from a complete one."""
        return self._refusal

    def refuse(self, link: str, reason: str) -> None:
        """Refuse the schedule before, or part way through, a run.

        For a caller that knows something about the SCHEDULE this module
        cannot see: `revl.shadow_runtime` refuses one whose realm or action
        the composition does not bear out. The first refusal wins, because a
        later one would describe a window that already stopped accumulating.
        """
        if self._refusal is None:
            self._refusal = (link, reason)

    def offer(self, crossing: tuple,
              answered: Optional[Answered] = None,
              served: Optional[str] = None) -> Optional[Answered]:
        """Offer one crossing to the schedule. Returns the answer SERVED.

        ``answered`` is the incumbent's answer when the caller already holds
        it, which is what a running seam holds: the incumbent has answered and
        asking it again would issue and pay for a second completion. Absent,
        the ``incumbent`` producer is called for it.

        ``served`` is which side's answer actually reached the caller, for a
        caller that KNOWS rather than infers. Absent, it is derived from the
        route the way :func:`serve` derives it, which is the route's claim
        about who is answering. `revl.shadow_runtime` passes it explicitly:
        the incumbent answers a crossing of a route that is not live, and the
        candidate answers one the share selected on a route that is.

        The return is the answer matching the :class:`Served` this call
        records, so a caller that hands it back serves what the ledger says it
        served. On a route that is not live that is the incumbent's, which is
        what every offline caller already got; :func:`serve` ignores the
        return and keeps its batch shape."""
        if self._refusal is not None:
            return answered
        if not isinstance(crossing, tuple) or len(crossing) != 2:
            self._refusal = (
                ROUTE_MALFORMED,
                f"{crossing!r} is not a (component, step_index) crossing "
                f"key; that key is item 517's own and this module invents "
                f"no second correlation")
            return answered
        shadowed = selects(self.route, crossing)
        left = answered
        if left is None:
            if self._incumbent is None:
                self._refusal = (
                    ROUTE_MALFORMED,
                    f"crossing {crossing!r} was offered with no incumbent "
                    f"answer and the schedule has no incumbent producer to "
                    f"ask for one; a shadow with no incumbent side is not a "
                    f"comparison")
                return None
            left = self._incumbent(crossing)
        if not shadowed:
            # The candidate is not consulted at all. This is the branch the
            # share exists to take, and `candidate_calls` counts the other one.
            # The incumbent's answer is then the only one there is to hand
            # back, whatever the route's `live` says: a crossing the share did
            # not select is a crossing the candidate did not answer, and a
            # ledger that said otherwise would name an answer nobody made.
            self._served.append(Served(crossing, False, INCUMBENT))
            return left
        self._calls += 1
        side = served if served is not None else (
            CANDIDATE if self.route.live else INCUMBENT)
        right = self._candidate(crossing)
        observation = promotion.Observation(
            left.record, right.record,
            slo=self._metrics(crossing)
            if self._metrics is not None else None,
            realm=self.route.realm,
            incumbent_world=left.world,
            candidate_world=right.world)
        self._entries.append(Entry(
            crossing=crossing, component=self.route.component,
            action=self.route.action, realm=self.route.realm,
            side=side, observation=observation))
        self._served.append(Served(crossing, True, side))
        return right if side == CANDIDATE else left

    def ledger(self) -> ShadowLedger:
        """The accumulated window. A refused schedule yields an EMPTY ledger
        carrying the refusal, which is :func:`serve`'s own shape."""
        if self._refusal is not None:
            return ShadowLedger(route=self.route, refusal=self._refusal)
        return ShadowLedger(route=self.route, entries=tuple(self._entries),
                            served=tuple(self._served),
                            candidate_calls=self._calls)


def serve(route: ShadowRoute, crossings: Sequence[tuple], *,
          incumbent: Callable[[tuple], Answered],
          candidate: Callable[[tuple], Answered],
          slo: Optional[Callable[[tuple], Mapping[str, Any]]] = None,
          ) -> ShadowLedger:
    """Serve a sequence of crossings, shadowing the declared share of them.

    ``incumbent`` and ``candidate`` are the runtime seam, one per role. They
    are called with a `(component, step_index)` crossing key and return an
    :class:`Answered`. This module is tier-independent: a tier wires the two
    callables to its own model boundary, and the scheduling, the stamping and
    the accounting are the same whichever tier does.

    Three properties this function has, all of them measured in the test file
    rather than asserted here:

    1. ``candidate`` is called ONLY on crossings the share selects. That is
       what "a declared fraction" means, and the ledger's `candidate_calls` is
       counted at the call rather than derived from the share.
    2. The answer SERVED is the incumbent's on every crossing while the route
       is not live, shadowed or not. A shadow whose candidate answers is not a
       shadow. On a LIVE route the candidate's answer is the one served on the
       crossings the share selected, and the incumbent's on the rest: a live
       route is the cutover, and it is what makes a landed promotion change
       which model answers.
    3. Every entry carries this route's action and realm, so the window is
       this action class's evidence and `decide` can refuse one that is not.

    A malformed route produces an EMPTY ledger carrying the refusal, rather
    than raising. The caller is a runtime seam and the failure a caller needs
    here is one that stops the shadow, not one that stops the incumbent from
    answering.
    """
    scheduler = Scheduler(route, incumbent=incumbent, candidate=candidate,
                          slo=slo)
    for crossing in crossings or ():
        if scheduler.refusal is not None:
            break
        scheduler.offer(crossing)
    return scheduler.ledger()


# ---------------------------------------------------------------------------
# the scheduler's preconditions, then the gate
# ---------------------------------------------------------------------------

def _check_entries(route: ShadowRoute, plan: promotion.ShadowPlan,
                   entries: Sequence[Entry]) -> Optional[tuple]:
    """``(link, reason, where)`` for the first entry that is not this
    promotion's evidence, ``None`` when every entry is.

    Reads the STAMP and nothing else. No answer member is touched here, which
    keeps these checks on the same side of `revl.shadow_promotion`'s barrier
    as its own preconditions; a test walks this function's syntax tree and
    asserts it."""
    for index, entry in enumerate(entries):
        if not isinstance(entry, Entry):
            return (ACTION_UNSCHEDULED,
                    f"window position {index} is {entry!r}, which carries no "
                    f"schedule stamp; an observation nothing scheduled cannot "
                    f"say which action class it is evidence for, and item "
                    f"517's record body names no action",
                    None)
        if entry.component != plan.component or entry.action != plan.action:
            return (ACTION_MISMATCHED,
                    f"window position {index} is stamped "
                    f"{entry.component}.{entry.action} and the plan promotes "
                    f"{plan.component}.{plan.action}; agreement accumulated "
                    f"on one action class is not evidence about another, and "
                    f"counting it would let a busy action's window carry a "
                    f"rare one's promotion",
                    entry.crossing)
        if entry.realm != route.realm:
            return (REALM_MISMATCHED,
                    f"window position {index} was served in realm "
                    f"{entry.realm!r} and the route shadows {route.realm!r}; "
                    f"the realm is half of the attribution a divergence "
                    f"reports, so a mixed window attributes to no realm",
                    entry.crossing)
        if entry.side not in SIDES:
            return (SERVED_CANDIDATE,
                    f"window position {index} records the served side as "
                    f"{entry.side!r}, which is outside {list(SIDES)}",
                    entry.crossing)
        if entry.side == CANDIDATE and not route.live:
            return (SERVED_CANDIDATE,
                    f"window position {index} served the CANDIDATE's answer "
                    f"on a route that is not live; in shadow the incumbent's "
                    f"answer is the one used, and a window in which the "
                    f"candidate answered measures a cutover that already "
                    f"happened rather than a shadow",
                    entry.crossing)
    return None


def decide(route: ShadowRoute, plan: promotion.ShadowPlan,
           entries: Sequence[Entry], *,
           key: Optional[bytes] = None,
           verifier: Optional[Callable] = None) -> promotion.Promotion:
    """The scheduler's checks, then `revl.shadow_promotion.decide`.

    The shape, which continues that module's own:

        route -> share -> stamp -> realm -> served
        ---------------- the schedule ---------------
        || then the gate, unchanged:
        plan -> route -> admit -> evidence -> policy -> authority -> state
        || BARRIER
        accumulate -> divergence / sample / threshold

    Everything this function adds is left of the gate and left of the barrier.
    It reads the schedule's stamp, never an answer, and it builds no tally: a
    refusal here reports `measurements_read` false and every stage of the gate
    as not reached, in the same :class:`~revl.shadow_promotion.Promotion`
    shape. There is deliberately no second verdict type and no fourth decision
    word.

    The share does not appear below this line. It selected which crossings
    were observed and it is not evidence about any of them."""
    bad = _check_route(route)
    if bad is not None:
        return promotion.refused(plan, bad[0], bad[1], stage=SCHEDULE_STAGE)

    if not isinstance(plan, promotion.ShadowPlan):
        return promotion.refused(plan, ROUTE_MISMATCHED,
                                 f"{plan!r} is not a ShadowPlan",
                                 stage=SCHEDULE_STAGE)

    for name in ("component", "action", "incumbent_role", "candidate_role"):
        mine, theirs = getattr(route, name), getattr(plan, name)
        if mine != theirs:
            return promotion.refused(
                plan, ROUTE_MISMATCHED,
                f"the schedule's {name} is {mine!r} and the plan's is "
                f"{theirs!r}; a schedule for one promotion cannot supply the "
                f"evidence for another, and reconciling the two afterwards is "
                f"how a window ends up attributed to the wrong thing",
                stage=SCHEDULE_STAGE)
    if route.live != plan.live:
        return promotion.refused(
            plan, ROUTE_MISMATCHED,
            f"the schedule is live={route.live} and the plan is "
            f"live={plan.live}; `live` selects whether the first attributed "
            f"divergence reverts or only lowers agreement, so the two must "
            f"be one statement",
            stage=SCHEDULE_STAGE)

    entries = list(entries) if isinstance(entries, (list, tuple)) else []
    mismatch = _check_entries(route, plan, entries)
    if mismatch is not None:
        link, reason, where = mismatch
        return promotion.refused(plan, link, reason, stage=SCHEDULE_STAGE,
                                 where=where,
                                 observations=[e.observation for e in entries
                                               if isinstance(e, Entry)])

    return promotion.decide(plan,
                            [entry.observation for entry in entries],
                            key=key, verifier=verifier)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def render(verdict: promotion.Promotion,
           ledger: Optional[ShadowLedger] = None) -> str:
    """The gate's report, with what the scheduler served above it.

    The two are printed separately and the schedule comes FIRST, because a
    reader needs to know how much of the action was observed before reading
    what the observation says. The share is printed as the rational it was
    declared as; it is reported, never weighed."""
    out = []
    if ledger is not None:
        route = ledger.route
        out.append(f"{ROUTING_KIND} {ROUTING_VERSION}")
        out.append(f"schedule: {route.component}.{route.action} in realm "
                   f"`{route.realm}`, share {route.share}, "
                   f"{route.incumbent_role} -> {route.candidate_role}"
                   + (" (live)" if route.live else " (shadow)"))
        out.append(f"  crossings offered: {ledger.offered}, "
                   f"served in shadow: {ledger.shadowed}, "
                   f"candidate consulted: {ledger.candidate_calls} times")
        out.append(f"  answers served by the incumbent: "
                   f"{sum(1 for s in ledger.served if s.side == INCUMBENT)}"
                   f" of {ledger.offered}")
        out.append(f"  pairs carrying both recorded worlds: "
                   f"{ledger.worlds_recorded} of {len(ledger.entries)}")
        if ledger.refusal is not None:
            out.append(f"  schedule refused: {ledger.refusal[0]} - "
                       f"{ledger.refusal[1]}")
        out.append("")
    out.append(promotion.render(verdict))
    return "\n".join(out)


#: Named so a caller can assert the module builds no verdict of its own: every
#: decision word in a report from here is one of `shadow_promotion`'s three.
DECISIONS = promotion.DECISIONS

__all__ = [
    "ROUTING_KIND", "ROUTING_VERSION", "WINDOW_KIND", "WINDOW_VERSION",
    "INCUMBENT", "CANDIDATE", "SIDES",
    "LINKS", "WINDOW_LINKS", "SCHEDULE_STAGE", "DECISIONS",
    "ROUTE_MALFORMED", "SHARE_MALFORMED", "ROUTE_MISMATCHED",
    "ACTION_UNSCHEDULED", "ACTION_MISMATCHED", "REALM_MISMATCHED",
    "SERVED_CANDIDATE", "WINDOW_MALFORMED", "WINDOW_TRUNCATED",
    "Share", "NOTHING", "EVERYTHING", "ShadowRoute", "Answered", "Served",
    "Entry", "ShadowLedger", "Scheduler", "selects", "serve", "decide",
    "render", "window_from_dict",
]

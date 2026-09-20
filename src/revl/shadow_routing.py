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
    correlation slice 1 does not derive. Measured on `dfecba2a`: a plan naming
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

#: Every link this module can report, for a caller that wants exhaustiveness.
LINKS = (
    ROUTE_MALFORMED, SHARE_MALFORMED, ROUTE_MISMATCHED,
    ACTION_UNSCHEDULED, ACTION_MISMATCHED, REALM_MISMATCHED,
    SERVED_CANDIDATE,
)

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
    of each it holds so a reader is never guessing which comparison ran."""

    record: Mapping[str, Any]
    world: Any = None


@dataclass(frozen=True)
class Served:
    """What one crossing did.

    ``side`` is which answer was handed back to the caller, and it is
    :data:`INCUMBENT` on every crossing of a route that is not live.
    ``shadowed`` says whether the candidate was consulted at all: on a
    crossing the share did not select, it was not called, and the ledger
    counts that rather than asserting it."""

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


# ---------------------------------------------------------------------------
# the scheduler
# ---------------------------------------------------------------------------

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
       shadow.
    3. Every entry carries this route's action and realm, so the window is
       this action class's evidence and `decide` can refuse one that is not.

    A malformed route produces an EMPTY ledger carrying the refusal, rather
    than raising. The caller is a runtime seam and the failure a caller needs
    here is one that stops the shadow, not one that stops the incumbent from
    answering.
    """
    bad = _check_route(route)
    if bad is not None:
        return ShadowLedger(route=route, refusal=bad)

    entries, served, calls = [], [], 0
    for crossing in crossings or ():
        if not isinstance(crossing, tuple) or len(crossing) != 2:
            return ShadowLedger(
                route=route, refusal=(
                    ROUTE_MALFORMED,
                    f"{crossing!r} is not a (component, step_index) crossing "
                    f"key; that key is item 517's own and this module invents "
                    f"no second correlation"))
        shadowed = selects(route, crossing)
        side = CANDIDATE if route.live else INCUMBENT
        left = incumbent(crossing)
        if not shadowed:
            # The candidate is not consulted at all. This is the branch the
            # share exists to take, and `candidate_calls` counts the other one.
            served.append(Served(crossing, False, side))
            continue
        calls += 1
        right = candidate(crossing)
        observation = promotion.Observation(
            left.record, right.record,
            slo=slo(crossing) if slo is not None else None,
            realm=route.realm,
            incumbent_world=left.world,
            candidate_world=right.world)
        entries.append(Entry(crossing=crossing, component=route.component,
                             action=route.action, realm=route.realm,
                             side=side, observation=observation))
        served.append(Served(crossing, True, side))
    return ShadowLedger(route=route, entries=tuple(entries),
                        served=tuple(served), candidate_calls=calls)


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
    "ROUTING_KIND", "ROUTING_VERSION", "INCUMBENT", "CANDIDATE", "SIDES",
    "LINKS", "SCHEDULE_STAGE", "DECISIONS",
    "ROUTE_MALFORMED", "SHARE_MALFORMED", "ROUTE_MISMATCHED",
    "ACTION_UNSCHEDULED", "ACTION_MISMATCHED", "REALM_MISMATCHED",
    "SERVED_CANDIDATE",
    "Share", "NOTHING", "EVERYTHING", "ShadowRoute", "Answered", "Served",
    "Entry", "ShadowLedger", "selects", "serve", "decide", "render",
]

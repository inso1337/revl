"""Shadow runtime: the seam between a shadow schedule and a COMPOSITION
(roadmap item 518, issue #1192).

Design note: `docs/design/558-shadow-scheduling.md`, sections 10 to 13. Slice 3
of `docs/design/540-shadow-promotion.md`.

WHAT SLICE 2 LEFT, AND WHY THEY WERE ONE THING
----------------------------------------------
`revl.shadow_routing` (PR #1297) schedules the shadow: it decides which
crossings are served, calls the candidate only on those, stamps each
observation with the action and the realm, and hands the window to
`revl.shadow_promotion`'s gate. Note 558 section 9 listed four things it did
not do, and all four had one cause, which the note says in its own words:
nothing was wired to a running composition.

  1. **No tier called `serve`.** The scheduler's two producers were a seam and
     nothing in the emitted runtimes reached it.
  2. **The recorded worlds were supplied, not built.** What was measured is
     that when they ARE supplied the comparison runs and changes the verdict.
  3. **The stamp was trusted, not derived.** Nothing checked the scheduler's
     claim about which action a crossing belonged to, "because `step_index`
     has no static producer in the tree".
  4. **The realm was declared, not resolved.** `revl canary` refuses a realm
     the composition does not have, through `placement.slice_partition`; the
     scheduler accepted any string.

This module closes them in that order, which is also the order of decreasing
difficulty, because each later one needs a composition in hand and (1) is what
puts one there.

THE TIER THAT IS WIRED, AND THE FIVE THAT ARE NOT
-------------------------------------------------
**python only.** `backends/python/runtime.py` grew one hook,
`revl_attach_shadow(observe)`, consulted at `validate_retry`, the single
runtime seam every model completion in that tier crosses (item 121 section
2.1), after the response has validated and with no path from it to the value
returned. :class:`TierShadow` wires that hook to a
`revl.shadow_routing.Scheduler`.

The **ts, rust, java, wasm and cordis(C)** tiers are NOT wired. Nothing in
this module or in this branch touches them, and no emitter changed, so there
is nothing an emitter could have dropped: the hook lives in the runtime shim
an emitted component imports, not in emitted output. A tier is wired by
growing the same hook at its own completion seam and handing it to this
module; the scheduling, the stamping and the accounting are tier-independent
and stay here.

WHAT "A STATIC PRODUCER FOR `step_index`" TURNED OUT TO BE
---------------------------------------------------------
`revl.mcp.canary.slice_timeline(ir, component)` walks a component's IR body in
source order and appends one `replay.Step` per boundary-relevant node through
`replay.Timeline._add`, which assigns `Step.index = len(self.steps)`. The live
recorder assigns the index the same way, in `Timeline.record_emission`. So the
static walk IS the producer: its emission steps' indices are the crossing keys
a run of that component produces, and each step's `detail["origin"]` names the
provide method it is reached from, which is the ACTION.

The derivation is therefore not a second correlation. It is the composition's
own account of the crossings it declares, read with item 496's own walker.

What it assumes, stated because it is an assumption and not a proof: that the
activation being observed runs each declared step once, in source order, which
is the same premise `revl canary` already makes when it calls the static walk
"the recorded world". Where a run departs from it, the departure is a crossing
the composition does not declare at that index, and :func:`check_stamps`
REFUSES it (`stamp-underived`) rather than guessing an action for it. The
failure direction is the one this family uses everywhere: an underivable
stamp refuses, it does not pass.

FAILURE DIRECTION
-----------------
Fail-closed, and left of `revl.shadow_routing`'s own refusals:

    composition -> realm -> action -> stamp
    ---------------- this module ----------------
    || then the schedule, unchanged:
    route -> share -> stamp -> realm -> served
    || then the gate, unchanged:
    plan -> route -> admit -> ... || BARRIER || accumulate -> threshold

  * a realm the composition does not have refuses (`realm-unknown`), naming
    the realms it does have;
  * a component outside the designated realm's slice refuses
    (`realm-unplaced`);
  * an action the component does not declare refuses (`action-undeclared`);
  * an action that crosses no boundary refuses (`action-uncrossed`), because a
    schedule over an action with no crossing shadows nothing and would report
    an empty window as agreement;
  * a crossing the composition declares for ANOTHER action refuses
    (`stamp-forged`), which is the scheduler's claim checked;
  * a crossing the composition declares at no index refuses
    (`stamp-underived`);
  * an observer fault at the tier seam refuses (`shadow-faulted`), because a
    window the incumbent answered and the shadow did not is short, not clean.

Every one is reported through `shadow_promotion.refused`, at the
`composition` stage, in the same `Promotion` shape with `measurements_read`
false. No new verdict type, no fourth decision word, no `G-` code.

WHAT IS UNCHANGED, DELIBERATELY
-------------------------------
The share is still a rational `n/d` and selection is still a keyed SHA-256 of
`(salt, component, step_index)`; nothing here introduces a PRNG. The candidate
is still called only on selected crossings, counted at the call site, and the
incumbent's answer is still the one served, and at the tier seam that is
structural, because the hook's return value is discarded. And the verdict is
still a replay comparison: an unresolvable comparator DIVERGES.

ONE THING THAT IS NOT INHERITED, AND WHY
----------------------------------------
Slice 2 DERIVES the side that was served from the route: `live` means the
candidate is answering, so an entry on a live route records `candidate`. An
offline caller has nothing better to go on. A seam does. This hook's return is
discarded, so the incumbent answered every crossing this module records, and
:class:`TierShadow` passes that to `Scheduler.offer` as a fact rather than
letting it be inferred. A route served through here is a shadow whatever its
`live` flag says; `live` selects the gate's RULE (the first attributed
divergence reverts) and not who answered. A route whose successor really
answers is a cutover, and this seam does not perform one.

PUBLIC SURFACE
--------------
``world_for(ir, component)``        : the recorded world, BUILT from the IR
``answered(ir, component, record)`` : one side's answer with its world built
``declared_actions(ir, component)`` : the provide-method names, from the IR
``crossing_actions(ir, component)`` : `{step_index: action}`, the derivation
``resolve(ir, route)``              : realm, slice and crossings, or a refusal
``check_stamps(resolution, entries)``: the scheduler's claim, checked
``decide(ir, route, plan, entries, ...)`` : composition, schedule, then gate
``TierShadow``                      : the python tier's seam, wired
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from . import shadow_promotion as promotion
from . import shadow_routing as routing
from .mcp.schema import provided_methods
from .placement import slice_partition, slice_realms

RUNTIME_KIND = "revl.shadow-runtime"

#: MAJOR.MINOR, additive within a MAJOR.
RUNTIME_VERSION = "1.0"

#: The tier whose completion seam this module wires. One name, spelled once,
#: so a caller that asks "which tiers shadow" gets an answer rather than an
#: implication. `revl.shadow_routing` is tier-independent; this is not.
WIRED_TIERS = ("python",)

#: The tiers that do NOT call the scheduler. Listed rather than left to be
#: inferred from the absence of the others, because "the runtime seam is two
#: callables" read as all six to at least one reader of slice 2.
UNWIRED_TIERS = ("cordis", "java", "rust", "ts", "wasm")


# ---------------------------------------------------------------------------
# refusal links
# ---------------------------------------------------------------------------
#
# This module's own, distinct from `shadow_routing.LINKS` and from
# `shadow_promotion.LINKS`, for the same reason those two are distinct from
# each other: these are refusals about the COMPOSITION, which neither of those
# modules has. None of them is a `G-` code; a seam refusing a window is not
# the checker refusing a program.

REALM_UNKNOWN = "realm-unknown"
REALM_UNPLACED = "realm-unplaced"
COMPONENT_UNKNOWN = "component-unknown"
ACTION_UNDECLARED = "action-undeclared"
ACTION_UNCROSSED = "action-uncrossed"
STAMP_UNDERIVED = "stamp-underived"
STAMP_FORGED = "stamp-forged"
SEAM_UNAVAILABLE = "seam-unavailable"
SHADOW_FAULTED = "shadow-faulted"

#: Every link this module can report, for a caller that wants exhaustiveness.
LINKS = (
    REALM_UNKNOWN, REALM_UNPLACED, COMPONENT_UNKNOWN, ACTION_UNDECLARED,
    ACTION_UNCROSSED, STAMP_UNDERIVED, STAMP_FORGED, SEAM_UNAVAILABLE,
    SHADOW_FAULTED,
)

#: The stage these refusals are reported under. It sits LEFT of
#: `shadow_routing.SCHEDULE_STAGE`, which sits left of the gate's own stages,
#: so a reader of a verdict can see how far the walk got.
COMPOSITION_STAGE = "composition"


# ---------------------------------------------------------------------------
# the two modules this one borrows rather than restates
# ---------------------------------------------------------------------------

def _canary():
    """`revl.mcp.canary` (item 496). Imported lazily because it reaches for
    the backend's replay engine, which a caller of this module's pure half
    does not need."""
    from .mcp import canary  # noqa: PLC0415 - lazy, pulls the backend path

    return canary


def _replay():
    """The backwards-replay engine's step vocabulary
    (`backends/python/replay.py`), so the emission kind is read from the
    module that defines it rather than spelled here."""
    from .mcp.session import replay_module  # noqa: PLC0415 - lazy

    return replay_module()


def tier_runtime():
    """The python tier's runtime shim, `backends/python/runtime.py`.

    Imported by path the way `revl.mcp.session.replay_module` imports the
    recorder beside it, and for the same reason: the shim needs no cordis, so
    the seam can be exercised without a runtime installed. Raises
    `ModuleNotFoundError` when the backend tree is not beside this package,
    which :class:`TierShadow` turns into a `seam-unavailable` refusal rather
    than letting it escape into a run."""
    from .mcp.session import backends_root  # noqa: PLC0415 - lazy

    backend_dir = backends_root() / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import runtime  # noqa: PLC0415 - backend import after path setup

    return runtime


# ---------------------------------------------------------------------------
# the recorded world, BUILT
# ---------------------------------------------------------------------------

def world_for(ir: Mapping[str, Any], component: str):
    """The recorded world of one component of ONE GENERATION, as a
    `replay.Timeline` in `revl.mcp.canary`'s own format.

    This is `canary.slice_timeline`, called and not reimplemented, so the
    steps, the `slot` detail and the comparison key stay item 496's and move
    when that one moves. Slice 2 accepted these two timelines from its caller;
    a shadow over a live composition has the two generations' IRs in hand and
    builds them, which is the difference between measuring that the comparison
    WOULD change a verdict and having it change one."""
    return _canary().slice_timeline(ir, component)


def answered(ir: Mapping[str, Any], component: str,
             record: Mapping[str, Any]) -> routing.Answered:
    """One side's :class:`~revl.shadow_routing.Answered`: its sealed item 517
    record, and its recorded world BUILT from that side's generation."""
    return routing.Answered(record=record, world=world_for(ir, component))


# ---------------------------------------------------------------------------
# the derivation: what the composition says a crossing belongs to
# ---------------------------------------------------------------------------

def _component_entry(ir: Mapping[str, Any], component: str) -> Optional[dict]:
    for comp in (ir or {}).get("components") or []:
        if isinstance(comp, Mapping) and comp.get("name") == component:
            return dict(comp)
    return None


def declared_actions(ir: Mapping[str, Any], component: str) -> frozenset:
    """Every provide-method name ``component`` declares, read off the IR.

    The same set `revl.model_route._action_names` reads off the AST when it
    checks that a `route model on <action>` names an action that exists. Read
    from the IR here because a seam holds a linked composition and not a
    parse tree, and the two are the same names: `lower` carries a provide
    block's methods through verbatim."""
    comp = _component_entry(ir, component)
    if comp is None:
        return frozenset()
    return frozenset(name for methods in provided_methods(comp).values()
                     for name in methods if name)


def _action_of(origin: Any) -> Optional[str]:
    """The provide method an `origin` string names, or ``None`` for a step
    recorded outside any provided method.

    `canary._walk_steps` writes `f"{component}:{key}.{method}"` inside a
    provided method and the bare component name for the activation body and
    the provision itself. The activation body is not an action: a crossing
    there belongs to the component's own start-up, and stamping it with an
    action would invent the correlation this module exists to derive."""
    if not isinstance(origin, str) or ":" not in origin:
        return None
    tail = origin.split(":", 1)[1]
    return tail.rsplit(".", 1)[1] if "." in tail else None


def crossing_actions(ir: Mapping[str, Any], component: str) -> dict:
    """``{step_index: action}`` for every boundary crossing the composition
    declares for ``component``.

    This is the static producer note 558 section 3.1 said the tree did not
    have. `canary.slice_timeline` walks the component's IR body in source
    order through `replay.Timeline._add`, which assigns
    `Step.index = len(self.steps)`, the same assignment the live recorder
    makes in `Timeline.record_emission`. So an emission step's index here is
    the `step_index` half of the `(component, step_index)` crossing key a run
    of this component mints, and its `origin` names the action.

    Only EMISSION steps are keyed. An effect or a provision is a step in the
    recorded world and not a boundary crossing, so a crossing key pointing at
    one is not a model completion and is refused rather than attributed.

    What this does NOT say is whether a crossing was a MODEL completion as
    opposed to some other emission; the composition does not distinguish them
    at this layer. The tier seam does: the hook fires at `validate_retry` and
    nowhere else, so every crossing that reaches the schedule is a completion
    by construction."""
    emission = _replay().KIND_EMISSION
    out: dict = {}
    for step in world_for(ir, component).steps:
        if step.kind != emission:
            continue
        detail = step.detail if isinstance(step.detail, dict) else {}
        out[step.index] = _action_of(detail.get("origin"))
    return out


# ---------------------------------------------------------------------------
# resolving the route against the composition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Resolution:
    """A shadow route, resolved against a linked composition.

    ``refusal`` is ``(link, reason)`` when the composition does not have what
    the route names, and ``None`` otherwise. It is carried rather than raised
    for `revl.shadow_routing`'s reason: the caller is a runtime seam, and the
    failure needed there stops the shadow and not the incumbent's answer."""

    route: routing.ShadowRoute
    members: tuple = ()
    providers: Mapping[str, str] = None  # type: ignore[assignment]
    remainder_realms: tuple = ()
    crossings: Mapping[int, Optional[str]] = None  # type: ignore[assignment]
    refusal: Optional[tuple] = None

    @property
    def ok(self) -> bool:
        return self.refusal is None

    def owns(self, crossing: Any) -> bool:
        """Whether the composition declares ``crossing`` for THIS route's
        action.

        The derivation, put to work at the seam rather than only checked
        after it. A component with two routed actions offers both actions'
        crossings to the same completion seam, and a schedule that stamped
        every one of them with its own action would manufacture exactly the
        miscorrelation note 558 section 1.1 measured. So a crossing the
        composition attributes elsewhere is never observed, never stamped and
        never counted: it is not this action class's evidence."""
        if self.refusal is not None:
            return False
        if not isinstance(crossing, tuple) or len(crossing) != 2:
            return False
        if crossing[0] != self.route.component:
            return False
        return (self.crossings or {}).get(crossing[1]) == self.route.action

    @property
    def scheduled(self) -> tuple:
        """The crossing keys the composition declares for THIS route's action,
        in index order. What a run of the component is expected to offer."""
        if not self.crossings:
            return ()
        return tuple((self.route.component, index)
                     for index, action in sorted(self.crossings.items())
                     if action == self.route.action)

    def as_dict(self) -> dict:
        return {
            "kind": RUNTIME_KIND, "version": RUNTIME_VERSION,
            "component": self.route.component,
            "action": self.route.action,
            "realm": self.route.realm,
            "members": list(self.members),
            "remainder_realms": list(self.remainder_realms),
            "declared_crossings": [list(c) for c in self.scheduled],
            "refusal": list(self.refusal) if self.refusal else None,
        }


def resolve(ir: Mapping[str, Any],
            route: routing.ShadowRoute) -> Resolution:
    """Resolve a shadow route against a linked composition.

    The realm half is `revl canary`'s, reused rather than restated:
    `placement.slice_partition(ir, realm)` is the same call `canary`'s
    `select_slice` makes, and an empty ``members`` is the same signal it reads
    as "no such realm". The refusal names the realms the composition does
    have, because a scheduler pointed at a realm that does not exist is a
    typo and the useful output of a typo is the list of correct spellings."""
    if not isinstance(route, routing.ShadowRoute):
        return Resolution(route=route, refusal=(
            routing.ROUTE_MALFORMED, f"{route!r} is not a ShadowRoute"))
    if not isinstance(ir, Mapping) or not ir.get("components"):
        return Resolution(route=route, refusal=(
            COMPONENT_UNKNOWN,
            "no linked composition was supplied, so nothing can be resolved "
            "against one; a route checked against no composition is the "
            "declared realm this slice exists to stop being trusted"))

    if _component_entry(ir, route.component) is None:
        known = ", ".join(sorted(
            c.get("name", "?") for c in ir.get("components") or []
            if isinstance(c, Mapping))) or "(none)"
        return Resolution(route=route, refusal=(
            COMPONENT_UNKNOWN,
            f"no component {route.component!r} in this composition; "
            f"it has: {known}"))

    part = slice_partition(ir, route.realm)
    if not part["members"]:
        known = ", ".join(slice_realms(ir)) or "(none)"
        return Resolution(route=route, refusal=(
            REALM_UNKNOWN,
            f"no realm {route.realm!r} in this composition; known realms: "
            f"{known}. A shadow is served into a realm for G2's reason, that "
            f"two providers of one key may not share one, so a realm that does "
            f"not exist is a shadow with nowhere to be served"))

    members = tuple(part["members"])
    if route.component not in members:
        return Resolution(route=route, refusal=(
            REALM_UNPLACED,
            f"{route.component} is not a member of realm {route.realm!r}; "
            f"that slice is {', '.join(members)}. The realm is half of the "
            f"`(component, realm)` attribution a divergence reports, and a "
            f"component attributed to a realm it is not isolated into "
            f"attributes to nothing"))

    declared = declared_actions(ir, route.component)
    if route.action not in declared:
        known = ", ".join(sorted(declared)) or "(none)"
        return Resolution(route=route, refusal=(
            ACTION_UNDECLARED,
            f"{route.component} declares no action {route.action!r}; it "
            f"declares: {known}. An action class is what agreement "
            f"accumulates under, so one the composition does not have "
            f"accumulates evidence about nothing"))

    crossings = crossing_actions(ir, route.component)
    resolution = Resolution(
        route=route, members=members, providers=dict(part["providers"]),
        remainder_realms=tuple(part["remainderRealms"]), crossings=crossings)
    if not resolution.scheduled:
        return Resolution(route=route, members=members,
                          providers=dict(part["providers"]),
                          remainder_realms=tuple(part["remainderRealms"]),
                          crossings=crossings, refusal=(
            ACTION_UNCROSSED,
            f"{route.component}.{route.action} crosses no boundary in this "
            f"composition, so a schedule over it shadows nothing; an empty "
            f"window reported as agreement is the fail-open shape this "
            f"family refuses everywhere else"))
    return resolution


# ---------------------------------------------------------------------------
# the stamp, checked against the composition
# ---------------------------------------------------------------------------

def check_stamps(resolution: Resolution,
                 entries: Sequence[routing.Entry]) -> Optional[tuple]:
    """``(link, reason, where)`` for the first entry whose stamp the
    composition does not bear out, ``None`` when every stamp is derivable.

    Slice 2's `shadow_routing._check_entries` checks the stamp against the
    PLAN: it catches a window assembled for one promotion and handed to
    another. It cannot catch a scheduler that stamped the wrong action,
    because both sides of that comparison are the scheduler's own word. This
    function is the other half: the composition's account of which action each
    crossing belongs to, against the scheduler's claim about it.

    That is the whole of note 558 section 3.1's "the stamp is trusted, not
    derived". It turns section 1.1's measured defect, a window of
    `summarize`'s crossings promoting `classify`, from something the
    scheduler is trusted not to do into something it cannot do."""
    declared = resolution.crossings or {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, routing.Entry):
            return (STAMP_UNDERIVED,
                    f"window position {index} is {entry!r}, which carries no "
                    f"schedule stamp to check against the composition",
                    None)
        crossing = entry.crossing
        if not isinstance(crossing, tuple) or len(crossing) != 2:
            return (STAMP_UNDERIVED,
                    f"window position {index} carries {crossing!r}, which is "
                    f"not a (component, step_index) crossing key",
                    None)
        if crossing[0] != resolution.route.component:
            return (STAMP_UNDERIVED,
                    f"window position {index} is a crossing of "
                    f"{crossing[0]!r} and the route shadows "
                    f"{resolution.route.component!r}",
                    crossing)
        if crossing[1] not in declared:
            keys = ", ".join(str(k) for k in sorted(declared)) or "(none)"
            return (STAMP_UNDERIVED,
                    f"window position {index} is crossing {crossing[1]} of "
                    f"{crossing[0]}, which this composition declares at no "
                    f"index; it declares boundary crossings at: {keys}. The "
                    f"action a crossing belongs to is derived from the "
                    f"composition's own step walk, and a crossing outside it "
                    f"has no derivable action to check the stamp against",
                    crossing)
        derived = declared.get(crossing[1])
        if derived != entry.action:
            named = (repr(derived) if derived is not None
                     else "the activation body, which is no action")
            return (STAMP_FORGED,
                    f"window position {index} is stamped "
                    f"{entry.component}.{entry.action} and the composition "
                    f"declares crossing {crossing[1]} of {crossing[0]} for "
                    f"{named}; the stamp is the scheduler's claim about "
                    f"which action class a crossing is evidence for, and "
                    f"this is that claim checked against the thing that "
                    f"would have to bear it out",
                    crossing)
    return None


# ---------------------------------------------------------------------------
# composition, then schedule, then gate
# ---------------------------------------------------------------------------

def decide(ir: Mapping[str, Any], route: routing.ShadowRoute,
           plan: promotion.ShadowPlan, entries: Sequence[routing.Entry], *,
           ledger: Optional[routing.ShadowLedger] = None,
           key: Optional[bytes] = None,
           verifier: Optional[Callable] = None) -> promotion.Promotion:
    """This module's checks, then `revl.shadow_routing.decide`.

    Everything added here is left of the schedule, which is left of the gate,
    which is left of the barrier. It reads the composition and the stamp,
    never an answer and never a metric, and it builds no tally: a refusal here
    reports `measurements_read` false and every stage after it as not reached,
    in the same `Promotion` shape slice 1 and slice 2 both return.

    ``ledger`` is optional and is read for ONE thing: a ledger that already
    carries a refusal (a faulted tier seam, a malformed route) reports that
    refusal here rather than being silently re-decided on the entries that
    happened to accumulate before it."""
    if ledger is not None and getattr(ledger, "refusal", None):
        return promotion.refused(plan, ledger.refusal[0], ledger.refusal[1],
                                 stage=COMPOSITION_STAGE)

    resolution = resolve(ir, route)
    if not resolution.ok:
        return promotion.refused(plan, resolution.refusal[0],
                                 resolution.refusal[1],
                                 stage=COMPOSITION_STAGE)

    entries = list(entries) if isinstance(entries, (list, tuple)) else []
    bad = check_stamps(resolution, entries)
    if bad is not None:
        link, reason, where = bad
        return promotion.refused(
            plan, link, reason, stage=COMPOSITION_STAGE, where=where,
            observations=[e.observation for e in entries
                          if isinstance(e, routing.Entry)])

    return routing.decide(route, plan, entries, key=key, verifier=verifier)


# ---------------------------------------------------------------------------
# the python tier's completion seam, wired
# ---------------------------------------------------------------------------

class TierShadow:
    """The python tier's model completion seam, wired to a shadow schedule.

    Use it as a context manager around the run being shadowed::

        with TierShadow(resolution, incumbent=seal_incumbent,
                        candidate=ask_candidate) as shadow:
            ...                       # the composition runs
        verdict = decide(ir, route, plan, shadow.ledger().entries,
                         ledger=shadow.ledger(), key=key)

    ``incumbent(crossing, value)`` turns the crossing and the RAW host return
    the incumbent just produced into an :class:`~revl.shadow_routing.Answered`
    (its sealed item 517 record and its recorded world). It is not asked to
    re-issue the completion, because the incumbent has already answered: that
    is the whole difference between a shadow and a replay, and re-issuing
    would charge for the incumbent's answer twice.

    ``candidate(crossing)`` issues the SUCCESSOR's answer to the same
    crossing. It is called only on the crossings the share selects, by
    `revl.shadow_routing.Scheduler`, which counts the calls at the call site.

    Three properties, each measured in `tests/test_shadow_runtime_518.py`:

    1. The value the body receives is the incumbent's, always. The hook's
       return is discarded by the seam, so there is no path from here to it.
    2. The candidate's own completion does not re-enter the seam: the tier
       holds a re-entrancy register for the duration of the hook.
    3. An observer fault stops the shadow and not the run. The tier catches
       it, detaches, and keeps it; this class turns a non-empty fault list
       into a `shadow-faulted` ledger, so the short window refuses instead of
       being decided as a clean one."""

    def __init__(self, resolution: Resolution, *,
                 incumbent: Callable[[tuple, Any], routing.Answered],
                 candidate: Callable[[tuple], routing.Answered],
                 slo: Optional[Callable[[tuple], Mapping[str, Any]]] = None,
                 runtime: Any = None):
        self.resolution = resolution
        self._incumbent = incumbent
        self._runtime = runtime
        self._attached = False
        self._observed = 0
        self._foreign = 0
        self._scheduler = routing.Scheduler(
            resolution.route, candidate=candidate, slo=slo)
        if not resolution.ok:
            # A route the composition does not bear out never reaches the
            # seam. Attaching it would shadow crossings of a realm or an
            # action that is not the one being promoted.
            self._scheduler.refuse(*resolution.refusal)

    # -- the hook ----------------------------------------------------------

    @property
    def observed(self) -> int:
        """How many completion crossings the seam offered, shadowed or not.
        Counted here rather than derived from the ledger, so a caller can see
        the seam firing even on a window the schedule refused."""
        return self._observed

    @property
    def foreign(self) -> int:
        """How many offered crossings the composition attributes to another
        action, and which this schedule therefore did not observe."""
        return self._foreign

    def observe(self, crossing: tuple, value: Any) -> None:
        """What the tier calls. Returns nothing, deliberately: the seam
        discards this return and the body gets the incumbent's answer.

        A crossing the composition does not attribute to this route's action
        is counted and dropped. That is the derivation acting rather than
        auditing: the incumbent still answers it, the successor is never
        asked about it, and it never enters a window that a threshold will be
        read against."""
        self._observed += 1
        if not self.resolution.owns(crossing):
            self._foreign += 1
            return
        self._scheduler.offer(crossing,
                              answered=self._incumbent(crossing, value),
                              served=routing.INCUMBENT)

    # -- attaching ---------------------------------------------------------

    def attach(self) -> "TierShadow":
        """Wire the seam. A backend tree that is not beside this package
        refuses the window (`seam-unavailable`) rather than raising into the
        run: a shadow that cannot be wired must not stop the incumbent."""
        runtime = self._runtime
        if runtime is None:
            try:
                runtime = self._runtime = tier_runtime()
            except ModuleNotFoundError as error:
                self._scheduler.refuse(
                    SEAM_UNAVAILABLE,
                    f"the python tier's runtime shim is not importable "
                    f"({error}), so there is no completion seam to wire and "
                    f"no crossing will be observed")
                return self
        runtime.revl_attach_shadow(self.observe)
        self._attached = True
        return self

    def detach(self) -> None:
        if self._attached and self._runtime is not None:
            self._runtime.revl_detach_shadow()
        self._attached = False

    def __enter__(self) -> "TierShadow":
        return self.attach()

    def __exit__(self, *_exc) -> bool:
        self.detach()
        return False

    # -- what happened -----------------------------------------------------

    @property
    def faults(self) -> tuple:
        """The tier's swallowed observer faults, `(crossing, repr(error))`."""
        if self._runtime is None:
            return ()
        return self._runtime.revl_shadow_faults()

    def ledger(self) -> routing.ShadowLedger:
        """The accumulated window, or an EMPTY one carrying the refusal.

        A fault at the seam is a refusal of the WHOLE window and not of the
        crossing it happened on, because the crossings that did accumulate
        are a biased sample of the ones that were offered: the run went on
        without them and nothing says the missing ones would have agreed."""
        ledger = self._scheduler.ledger()
        faults = self.faults
        if ledger.refusal is None and faults:
            crossing, error = faults[0]
            return routing.ShadowLedger(route=self.resolution.route, refusal=(
                SHADOW_FAULTED,
                f"the shadow observer faulted at crossing {crossing!r} "
                f"({error}) and the tier detached it; the incumbent answered "
                f"every crossing after that and the shadow observed none of "
                f"them, so this window is short rather than clean "
                f"({len(faults)} fault(s), {self._observed} crossings "
                f"offered)"))
        return ledger


#: Named so a caller can assert this module builds no verdict of its own.
DECISIONS = promotion.DECISIONS

__all__ = [
    "RUNTIME_KIND", "RUNTIME_VERSION", "WIRED_TIERS", "UNWIRED_TIERS",
    "LINKS", "COMPOSITION_STAGE", "DECISIONS",
    "REALM_UNKNOWN", "REALM_UNPLACED", "COMPONENT_UNKNOWN",
    "ACTION_UNDECLARED", "ACTION_UNCROSSED", "STAMP_UNDERIVED",
    "STAMP_FORGED", "SEAM_UNAVAILABLE", "SHADOW_FAULTED",
    "Resolution", "TierShadow", "answered", "check_stamps",
    "crossing_actions", "declared_actions", "decide", "resolve",
    "tier_runtime", "world_for",
]

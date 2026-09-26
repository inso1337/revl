"""Shadow register: a promotion that LANDS, and a revert that is performed
(roadmap item 518, issue #1192).

Design note: `docs/design/558-shadow-scheduling.md`, section 17.

WHAT WAS MISSING
----------------
`revl.shadow_promotion` turns a window into a verdict, `revl.shadow_routing`
schedules the window and `revl.shadow_runtime` wires it to the python tier's
completion seam. All three are pure: a `PROMOTE` verdict changed nothing, and
a `REVERT` verdict carried a `restoration` report saying `restored:
route-arm` about an arm no code had moved. The `live` flag that selects the
revert rule was set by whoever built the route, so nothing tied "the
candidate is in use" to a promotion having happened.

This module is the state those verdicts are about. It holds, per ACTION
CLASS `(component, action)`, the arm in use and a LIFO stack of the
promotions that put it there. Three operations change it and each one is
decided by the gate, not by the caller:

* :meth:`PromotionRegister.land` runs the gate over a shadow window and, on
  `PROMOTE`, pushes the candidate onto that class's stack with the comparison
  attached. The caller supplies evidence, never a verdict.
* :meth:`PromotionRegister.observe` runs the gate over a window taken AFTER
  the promotion, under the live rule, and on the first attributed divergence
  performs the revert.
* :meth:`PromotionRegister.revert` is that revert: it pops the top of the
  class's stack, supersedes the window that promoted it, and measures that
  every other class's state is unchanged.

:meth:`PromotionRegister.route` derives the `ShadowRoute` for a class from the
register, so the `live` flag a window is scheduled under is the register's
statement and not the caller's.

THE GATE IS EVIDENCE
--------------------
A verdict with no evidence is not a pass. The gate already refuses an empty
window (`evidence-missing`), and this module does not rely on that alone: a
`PROMOTE` whose tally paired nothing is refused here too (`evidence-empty`).
The per-class report prints a class with no decided window, or a window that
paired nothing, as NO EVIDENCE and gives it no agreement figure at all, not
0% and not 100%.

AGREEMENT IS PER ACTION CLASS
-----------------------------
Every operation takes one class and reads only that class's window; the gate
refuses a window stamped for another class. There is no operation that
promotes a role across a component's actions, so a successor that agreed on
`summarize` has no path to `classify`'s arm.

WHAT A REVERT DOES, AND WHAT IT ONLY REPORTS
---------------------------------------------
Three layers, as `revl.shadow_promotion` names them:

* `route-arm`: RESTORED. The class's state after the revert is compared with
  its state before the promotion landed, and `restored_exactly` is that
  comparison, not an assertion.
* `agreement-ledger`: COMPENSATED. The window that promoted is marked
  superseded, and :meth:`land` refuses a superseded window, so a reverted
  promotion cannot be re-landed on the evidence that promoted it.
* anything else the plan declared (`placement-history` in practice): NOT
  PERFORMED by this register, reported with the compensation the plan named.
  This register does not own that store and does not claim to have touched
  it.

Other action classes are the survivors. Their states are snapshotted before
and after, and a revert reports the exact set that is unchanged and the set
that changed (`breached`), which is empty by construction and measured anyway.

WHAT THIS DOES NOT DO
---------------------
No tier consults the register to choose which model answers. Item 512's route
is a permission and item 515 owns scheduling inside it
(`revl.model_route`), and the python seam discards the observer's return, so
the incumbent's completion is the one the body receives before and after a
promotion lands. The register is the declared arm and the gate's rule. It is
not a cutover, and until a tier routes by it a promotion does not change which
model answers a call; that is the part of item 518 still open.

NO NEW GUARANTEE CODE
---------------------
Every refusal is a named link, as in the three modules this one sits on. None
starts with `G-`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from . import shadow_promotion as promotion
from . import shadow_routing as routing
from . import shadow_runtime as runtime

REGISTER_KIND = "revl.shadow-register"

#: MAJOR.MINOR, additive within a MAJOR.
REGISTER_VERSION = "1.0"

#: The layer this register restores, and the one it compensates. Named from
#: `shadow_promotion.PROMOTION_LAYERS` so the vocabulary is that module's.
RESTORED_LAYER = "route-arm"
COMPENSATED_LAYER = "agreement-ledger"

#: What the register does to the agreement ledger on a revert.
SUPERSEDE = "supersede-window"

# ---------------------------------------------------------------------------
# refusal links
# ---------------------------------------------------------------------------

CLASS_UNDECLARED = "class-undeclared"
ARM_MISMATCHED = "arm-mismatched"
NOT_PROMOTED = "not-promoted"
WINDOW_SUPERSEDED = "window-superseded"
EVIDENCE_EMPTY = "evidence-empty"
VERDICT_NOT_REVERT = "verdict-not-revert"
REGISTER_MALFORMED = "register-malformed"

LINKS = (CLASS_UNDECLARED, ARM_MISMATCHED, NOT_PROMOTED, WINDOW_SUPERSEDED,
         EVIDENCE_EMPTY, VERDICT_NOT_REVERT, REGISTER_MALFORMED)

#: The three operations, as they appear in the history.
LAND, OBSERVE, REVERT = "land", "observe", "revert"


# ---------------------------------------------------------------------------
# values
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Arm:
    """One landed promotion: the class moved from ``from_role`` to
    ``to_role`` on the window ``window``, and ``comparison`` is the evidence
    that moved it, attached verbatim."""

    sequence: int
    action_class: tuple
    from_role: str
    to_role: str
    window: str
    comparison: Mapping[str, Any]
    before: Mapping[str, Any]
    declared_layers: Mapping[str, str]
    declared_compensations: Mapping[str, Any]

    def as_dict(self) -> dict:
        return {"sequence": self.sequence,
                "action_class": list(self.action_class),
                "from_role": self.from_role, "to_role": self.to_role,
                "window": self.window, "comparison": self.comparison,
                "before": self.before,
                "declared_layers": dict(self.declared_layers),
                "declared_compensations": dict(self.declared_compensations)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Arm":
        return cls(sequence=data["sequence"],
                   action_class=tuple(data["action_class"]),
                   from_role=data["from_role"], to_role=data["to_role"],
                   window=data["window"], comparison=data["comparison"],
                   before=data["before"],
                   declared_layers=dict(data["declared_layers"]),
                   declared_compensations=dict(
                       data["declared_compensations"]))


@dataclass(frozen=True)
class Outcome:
    """What one operation did. ``changed`` is whether the register's state
    moved, and it is false on every refusal."""

    operation: str
    action_class: tuple
    changed: bool
    verdict: Optional[promotion.Promotion] = None
    refusal: Optional[tuple] = None
    arm: Optional[Arm] = None
    reversal: Optional[Mapping[str, Any]] = None

    def as_dict(self) -> dict:
        return {"operation": self.operation,
                "action_class": list(self.action_class),
                "changed": self.changed,
                "verdict": self.verdict.as_dict() if self.verdict else None,
                "refusal": list(self.refusal) if self.refusal else None,
                "arm": self.arm.as_dict() if self.arm else None,
                "reversal": self.reversal}


# ---------------------------------------------------------------------------
# the window: identity and the comparison a landing carries
# ---------------------------------------------------------------------------

def window_id(entries: Sequence[routing.Entry]) -> str:
    """A digest of the window's crossings and both sides' sealed records.

    Two windows with the same records are the same evidence, whatever object
    carries them, so this is what a revert supersedes and what :meth:`land`
    checks a window against."""
    rows = [[list(e.crossing), e.observation.incumbent,
             e.observation.candidate] for e in entries]
    payload = json.dumps(rows, sort_keys=True, default=repr)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _legs(entry: routing.Entry) -> list:
    legs = ["records"]
    if entry.observation.has_world:
        legs.append(promotion.WORLD_COMPARISON)
    return legs


def comparison(verdict: promotion.Promotion,
               ledger: routing.ShadowLedger) -> dict:
    """The comparison a landed promotion carries: the verdict whole, what
    the scheduler served, and which comparison legs ran on which crossing."""
    return {
        "verdict": verdict.as_dict(),
        "schedule": ledger.as_dict(),
        "compared": [{"crossing": list(e.crossing), "legs": _legs(e)}
                     for e in ledger.entries],
    }


def _agreement(tally: Optional[promotion.Tally]) -> Optional[float]:
    """The ratio, or ``None`` when nothing was paired. ``Tally.agreement``
    returns 0.0 on an empty tally; a report must not print either 0% or 100%
    for a window that compared nothing."""
    if tally is None or tally.paired == 0:
        return None
    return tally.agreement


def _evidence_row(operation: str, verdict: promotion.Promotion) -> dict:
    tally = verdict.tally
    return {"operation": operation, "decision": verdict.decision,
            "link": verdict.refusal.link if verdict.refusal else None,
            "paired": tally.paired if tally else 0,
            "agreed": tally.agreed if tally else 0,
            "agreement": _agreement(tally)}


# ---------------------------------------------------------------------------
# the register
# ---------------------------------------------------------------------------

class PromotionRegister:
    """The arm in use per action class, and the promotions that moved it.

    ``arms`` declares every class the register governs and the role each one
    starts on. A class not declared here cannot be landed, observed or
    reverted: there is no default arm."""

    def __init__(self, arms: Mapping[tuple, str]):
        self._base: dict = {}
        for action_class, role in dict(arms).items():
            _check_class(action_class)
            if not isinstance(role, str) or not role:
                raise promotion.PromotionRefused(
                    REGISTER_MALFORMED,
                    f"{action_class} starts on {role!r}, which is not a role")
            self._base[tuple(action_class)] = role
        if not self._base:
            raise promotion.PromotionRefused(
                REGISTER_MALFORMED, "a register governing no action class "
                "governs nothing")
        self._stacks: dict = {c: [] for c in self._base}
        self._evidence: dict = {c: [] for c in self._base}
        self._superseded: set = set()
        self._history: list = []
        self._sequence = 0

    # -- reading -----------------------------------------------------------

    @property
    def classes(self) -> tuple:
        return tuple(sorted(self._base))

    def arm(self, action_class: tuple) -> str:
        """The role this class is on now."""
        stack = self._stacks[tuple(action_class)]
        return stack[-1].to_role if stack else self._base[tuple(action_class)]

    def promoted(self, action_class: tuple) -> Optional[Arm]:
        """The promotion on top of this class's stack, or ``None``."""
        stack = self._stacks[tuple(action_class)]
        return stack[-1] if stack else None

    def state(self, action_class: tuple) -> dict:
        """One class's state, as a value two snapshots can be compared by."""
        action_class = tuple(action_class)
        return {"base": self._base[action_class],
                "arm": self.arm(action_class),
                "stack": [a.sequence for a in self._stacks[action_class]]}

    def superseded(self, window: str) -> bool:
        return window in self._superseded

    def route(self, action_class: tuple, *, realm: str,
              share: routing.Share, salt: str = "",
              candidate_role: Optional[str] = None) -> routing.ShadowRoute:
        """The route a window for this class is scheduled under.

        Not promoted: the arm in use is the incumbent, ``candidate_role`` is
        shadowed against it, and the route is a shadow. Promoted, with no
        candidate named or the promoted one: the two roles are the
        promotion's own and the route is LIVE, so the first attributed
        divergence reverts. Promoted, with a different candidate named: a
        shadow of that candidate against the arm now in use, which is how a
        second promotion is stacked on the first. ``live`` is the register's
        statement, not the caller's."""
        action_class = self._known(action_class)
        top = self.promoted(action_class)
        if top is not None and candidate_role in (None, top.to_role):
            incumbent, candidate, live = top.from_role, top.to_role, True
        else:
            incumbent, candidate, live = (self.arm(action_class),
                                          candidate_role, False)
        return routing.ShadowRoute(
            component=action_class[0], action=action_class[1], realm=realm,
            incumbent_role=incumbent, candidate_role=candidate, share=share,
            salt=salt, live=live)

    # -- landing -----------------------------------------------------------

    def land(self, ir: Mapping[str, Any], ledger: routing.ShadowLedger,
             plan: promotion.ShadowPlan, *, key: Optional[bytes] = None,
             verifier: Optional[Callable] = None) -> Outcome:
        """Decide a shadow window and, on `PROMOTE`, land it.

        The verdict is computed here, by `revl.shadow_runtime.decide`, from
        the window the caller supplies. There is no parameter for a verdict,
        so a caller cannot land one the gate did not reach."""
        action_class = _class_of(ledger)
        refusal = self._land_refusal(action_class, ledger)
        if refusal is not None:
            return Outcome(LAND, action_class or (None, None), False,
                           refusal=refusal)
        verdict = runtime.decide(ir, ledger.route, plan, ledger.entries,
                                 ledger=ledger, key=key, verifier=verifier)
        self._evidence[action_class].append(_evidence_row(LAND, verdict))
        if verdict.decision != promotion.PROMOTE:
            link = verdict.refusal.link if verdict.refusal else "unnamed"
            reason = verdict.refusal.reason if verdict.refusal else ""
            return Outcome(LAND, action_class, False, verdict=verdict,
                           refusal=(link, reason))
        if verdict.tally is None or verdict.tally.paired == 0:
            return Outcome(LAND, action_class, False, verdict=verdict,
                           refusal=(EVIDENCE_EMPTY,
                                    "the gate said PROMOTE over a window that "
                                    "paired nothing; a verdict with no "
                                    "evidence is not a pass"))
        arm = self._push(action_class, ledger, plan, verdict)
        return Outcome(LAND, action_class, True, verdict=verdict, arm=arm)

    def _land_refusal(self, action_class: Optional[tuple],
                      ledger: routing.ShadowLedger) -> Optional[tuple]:
        if action_class is None or action_class not in self._base:
            return (CLASS_UNDECLARED,
                    f"{action_class!r} is not a class this register governs; "
                    f"it governs {_names(self.classes)}")
        route = ledger.route
        if route.live:
            return (ARM_MISMATCHED,
                    f"the window for {_name(action_class)} was scheduled "
                    f"live; a promotion lands on a SHADOW window, and a live "
                    f"one is observed with `observe`")
        if route.incumbent_role != self.arm(action_class):
            return (ARM_MISMATCHED,
                    f"the window treats {route.incumbent_role!r} as the "
                    f"incumbent and {_name(action_class)} is on "
                    f"{self.arm(action_class)!r}; the verdict would be about "
                    f"an arm that is not in use")
        window = window_id(ledger.entries)
        if window in self._superseded:
            return (WINDOW_SUPERSEDED,
                    f"window {window[:16]} promoted {_name(action_class)} "
                    f"once and was superseded by a revert; the evidence that "
                    f"was overturned cannot land the same promotion again")
        return None

    def _push(self, action_class: tuple, ledger: routing.ShadowLedger,
              plan: promotion.ShadowPlan,
              verdict: promotion.Promotion) -> Arm:
        self._sequence += 1
        arm = Arm(sequence=self._sequence, action_class=action_class,
                  from_role=verdict.incumbent_role,
                  to_role=verdict.candidate_role,
                  window=window_id(ledger.entries),
                  comparison=comparison(verdict, ledger),
                  before=self.state(action_class),
                  declared_layers=dict(plan.layers),
                  declared_compensations={
                      k: dict(v) for k, v in plan.compensations.items()})
        self._stacks[action_class].append(arm)
        self._history.append({"operation": LAND, "sequence": arm.sequence,
                              "action_class": list(action_class),
                              "from_role": arm.from_role,
                              "to_role": arm.to_role, "window": arm.window})
        return arm

    # -- observing a live promotion -----------------------------------------

    def observe(self, ir: Mapping[str, Any], ledger: routing.ShadowLedger,
                plan: promotion.ShadowPlan, *, key: Optional[bytes] = None,
                verifier: Optional[Callable] = None) -> Outcome:
        """Decide a window taken after the promotion, under the live rule.

        On `REVERT` the revert is performed and the outcome carries it. Any
        other verdict leaves the promotion standing and says so; a window
        that paired nothing is reported as no evidence, not as agreement."""
        action_class = _class_of(ledger)
        refusal = self._observe_refusal(action_class, ledger)
        if refusal is not None:
            return Outcome(OBSERVE, action_class or (None, None), False,
                           refusal=refusal)
        verdict = runtime.decide(ir, ledger.route, plan, ledger.entries,
                                 ledger=ledger, key=key, verifier=verifier)
        self._evidence[action_class].append(_evidence_row(OBSERVE, verdict))
        if verdict.decision != promotion.REVERT:
            refusal = ((verdict.refusal.link, verdict.refusal.reason)
                       if verdict.refusal else None)
            return Outcome(OBSERVE, action_class, False, verdict=verdict,
                           refusal=refusal, arm=self.promoted(action_class))
        outcome = self.revert(verdict)
        return Outcome(OBSERVE, action_class, outcome.changed,
                       verdict=verdict, refusal=outcome.refusal,
                       reversal=outcome.reversal)

    def _observe_refusal(self, action_class: Optional[tuple],
                         ledger: routing.ShadowLedger) -> Optional[tuple]:
        if action_class is None or action_class not in self._base:
            return (CLASS_UNDECLARED,
                    f"{action_class!r} is not a class this register governs")
        top = self.promoted(action_class)
        if top is None:
            return (NOT_PROMOTED,
                    f"{_name(action_class)} is on its base arm "
                    f"{self.arm(action_class)!r}; there is no promotion to "
                    f"observe, and a shadow window is landed with `land`")
        route = ledger.route
        if not route.live or (route.incumbent_role, route.candidate_role) \
                != (top.from_role, top.to_role):
            return (ARM_MISMATCHED,
                    f"the window was scheduled {route.incumbent_role!r} -> "
                    f"{route.candidate_role!r} live={route.live}, and "
                    f"{_name(action_class)} is promoted {top.from_role!r} -> "
                    f"{top.to_role!r}; schedule it with `route`")
        return None

    # -- reverting ---------------------------------------------------------

    def revert(self, verdict: promotion.Promotion) -> Outcome:
        """Undo the top promotion of the verdict's class.

        Only a `REVERT` verdict with an attributed divergence reverts, and
        only the promotion it is about: the verdict's two roles must be the
        top of the stack's, so a revert is LIFO and cannot reach under a
        later promotion."""
        action_class = tuple(getattr(verdict, "action_class", ()) or ())
        refusal = self._revert_refusal(action_class, verdict)
        if refusal is not None:
            return Outcome(REVERT, action_class or (None, None), False,
                           verdict=verdict if isinstance(
                               verdict, promotion.Promotion) else None,
                           refusal=refusal)
        others = [c for c in self._base if c != action_class]
        before = {c: self.state(c) for c in others}
        arm = self._stacks[action_class].pop()
        self._superseded.add(arm.window)
        after = {c: self.state(c) for c in others}
        reversal = _reversal(arm, verdict.tally.first_divergence,
                             self.state(action_class), before, after)
        self._history.append({"operation": REVERT, "sequence": arm.sequence,
                              "action_class": list(action_class),
                              "restored_role": arm.from_role,
                              "withdrawn_role": arm.to_role,
                              "window": arm.window,
                              "divergence": reversal["divergence"]})
        return Outcome(REVERT, action_class, True, verdict=verdict,
                       reversal=reversal)

    def _revert_refusal(self, action_class: tuple,
                        verdict: Any) -> Optional[tuple]:
        if not isinstance(verdict, promotion.Promotion) \
                or verdict.decision != promotion.REVERT \
                or verdict.refusal is None \
                or verdict.refusal.link != promotion.DIVERGENCE_ATTRIBUTED \
                or verdict.tally is None \
                or verdict.tally.first_divergence is None:
            return (VERDICT_NOT_REVERT,
                    "a revert answers an attributed divergence, and this "
                    "verdict does not carry one")
        if action_class not in self._base:
            return (CLASS_UNDECLARED,
                    f"{action_class!r} is not a class this register governs")
        top = self.promoted(action_class)
        if top is None:
            return (NOT_PROMOTED,
                    f"{_name(action_class)} is on its base arm; there is no "
                    f"promotion to revert")
        if (verdict.incumbent_role, verdict.candidate_role) \
                != (top.from_role, top.to_role):
            return (ARM_MISMATCHED,
                    f"the verdict is about {verdict.incumbent_role!r} -> "
                    f"{verdict.candidate_role!r} and the promotion on top of "
                    f"{_name(action_class)} is {top.from_role!r} -> "
                    f"{top.to_role!r}; a revert is LIFO")
        return None

    # -- reporting ---------------------------------------------------------

    def evidence(self) -> dict:
        """Per class: the arm, whether it is promoted, and every window the
        register decided for it. ``last`` is ``None`` for a class no window
        was ever decided for."""
        out = {}
        for action_class in self.classes:
            rows = list(self._evidence[action_class])
            out[_name(action_class)] = {
                "arm": self.arm(action_class),
                "promoted": self.promoted(action_class) is not None,
                "windows": rows,
                "last": rows[-1] if rows else None}
        return out

    @property
    def history(self) -> tuple:
        return tuple(dict(h) for h in self._history)

    def as_dict(self) -> dict:
        return {
            "kind": REGISTER_KIND, "version": REGISTER_VERSION,
            "classes": [{"action_class": list(c), "base": self._base[c],
                         "stack": [a.as_dict() for a in self._stacks[c]],
                         "evidence": list(self._evidence[c])}
                        for c in self.classes],
            "superseded": sorted(self._superseded),
            "history": list(self._history),
            "sequence": self._sequence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PromotionRegister":
        if not isinstance(data, Mapping) or data.get("kind") != REGISTER_KIND:
            raise promotion.PromotionRefused(
                REGISTER_MALFORMED, "not a serialised shadow register")
        classes = data.get("classes") or []
        register = cls({tuple(c["action_class"]): c["base"]
                        for c in classes})
        for entry in classes:
            action_class = tuple(entry["action_class"])
            register._stacks[action_class] = [Arm.from_dict(a)
                                              for a in entry["stack"]]
            register._evidence[action_class] = list(entry["evidence"])
        register._superseded = set(data.get("superseded") or ())
        register._history = list(data.get("history") or ())
        register._sequence = int(data.get("sequence") or 0)
        return register

    def _known(self, action_class: Any) -> tuple:
        action_class = tuple(action_class) if isinstance(
            action_class, (list, tuple)) else action_class
        if action_class not in self._base:
            raise promotion.PromotionRefused(
                CLASS_UNDECLARED,
                f"{action_class!r} is not a class this register governs; it "
                f"governs {_names(self.classes)}")
        return action_class


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _check_class(action_class: Any) -> None:
    if not isinstance(action_class, tuple) or len(action_class) != 2 \
            or not all(isinstance(p, str) and p for p in action_class):
        raise promotion.PromotionRefused(
            REGISTER_MALFORMED,
            f"{action_class!r} is not a (component, action) class")


def _class_of(ledger: Any) -> Optional[tuple]:
    route = getattr(ledger, "route", None)
    if not isinstance(route, routing.ShadowRoute):
        return None
    return route.action_class


def _name(action_class: tuple) -> str:
    return f"{action_class[0]}.{action_class[1]}"


def _names(classes: Sequence[tuple]) -> str:
    return ", ".join(_name(c) for c in classes) or "(none)"


def _not_performed(arm: Arm) -> list:
    """Layers the plan declared that this register neither restores nor
    compensates, with the compensation the plan named for each."""
    out = []
    for layer in promotion.PROMOTION_LAYERS:
        if layer in (RESTORED_LAYER, COMPENSATED_LAYER):
            continue
        declared = arm.declared_compensations.get(layer) or {}
        out.append({"layer": layer,
                    "state": arm.declared_layers.get(layer),
                    "declared_compensation": declared.get("name")})
    return out


def _reversal(arm: Arm, divergence: promotion.Divergence,
              restored_state: dict, before: dict, after: dict) -> dict:
    survivors = sorted(_name(c) for c in before if before[c] == after[c])
    breached = sorted(_name(c) for c in before if before[c] != after[c])
    return {
        "action_class": list(arm.action_class),
        "restored_role": arm.from_role,
        "withdrawn_role": arm.to_role,
        "divergence": divergence.as_dict(),
        "named": divergence.describe(),
        "restored": [RESTORED_LAYER],
        "restored_exactly": restored_state == dict(arm.before),
        "compensated": [{"layer": COMPENSATED_LAYER, "performed": SUPERSEDE,
                         "window": arm.window}],
        "not_performed": _not_performed(arm),
        "survivors": survivors,
        "breached": breached,
    }


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _render_last(last: Optional[Mapping[str, Any]]) -> str:
    if last is None:
        return "no evidence (no window decided)"
    if not last.get("paired"):
        return (f"no evidence (0 pairs; {last['decision']}"
                + (f", {last['link']}" if last.get("link") else "") + ")")
    return (f"{last['agreed']}/{last['paired']} agreed "
            f"({last['agreement']:.4f}), {last['decision']}"
            + (f", {last['link']}" if last.get("link") else ""))


def render(register: PromotionRegister) -> str:
    """One line per class: the arm, and the last window's evidence."""
    out = [f"{REGISTER_KIND} {REGISTER_VERSION}"]
    for name, row in register.evidence().items():
        state = "promoted" if row["promoted"] else "base arm"
        out.append(f"{name}: {row['arm']} ({state}); last window: "
                   f"{_render_last(row['last'])}")
    return "\n".join(out)


def render_reversal(reversal: Mapping[str, Any]) -> str:
    """A revert, as separate sentences for what was restored, what was only
    compensated and what this register did not touch."""
    cls = ".".join(reversal["action_class"])
    out = [f"reverted {cls}: {reversal['withdrawn_role']} -> "
           f"{reversal['restored_role']}",
           f"diverged at {reversal['named']}",
           f"restored: {', '.join(reversal['restored'])} (exactly: "
           f"{'yes' if reversal['restored_exactly'] else 'NO'})"]
    for entry in reversal["compensated"]:
        out.append(f"compensated only: {entry['layer']} via "
                   f"{entry['performed']} (window {entry['window'][:16]})")
    for entry in reversal["not_performed"]:
        out.append(f"not performed by this register: {entry['layer']} "
                   f"({entry['state']}, declared compensation "
                   f"{entry['declared_compensation']})")
    out.append("other action classes unchanged: "
               + (", ".join(reversal["survivors"]) or "none"))
    if reversal["breached"]:
        out.append("other action classes CHANGED: "
                   + ", ".join(reversal["breached"]))
    return "\n".join(out)


#: Named so a caller can assert this module builds no verdict of its own.
DECISIONS = promotion.DECISIONS

__all__ = [
    "REGISTER_KIND", "REGISTER_VERSION", "LINKS", "DECISIONS",
    "CLASS_UNDECLARED", "ARM_MISMATCHED", "NOT_PROMOTED", "WINDOW_SUPERSEDED",
    "EVIDENCE_EMPTY", "VERDICT_NOT_REVERT", "REGISTER_MALFORMED",
    "RESTORED_LAYER", "COMPENSATED_LAYER", "SUPERSEDE",
    "Arm", "Outcome", "PromotionRegister", "comparison", "window_id",
    "render", "render_reversal",
]

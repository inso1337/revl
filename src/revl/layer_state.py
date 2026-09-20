"""Evolvable layer state: what a rollback RESTORES and what it only COMPENSATES.

Roadmap item 546 (issue #1225). Design note:
`docs/design/547-evolvable-layer-state.md`.

THE GAP
-------
The evolution lifecycle ends in "promote or roll back", and rollback is the
half revl makes trivial: a revertible effect carries its inverse, unloading a
component replays the inverses in LIFO order, removal leaves no residue.

That guarantee is about EFFECTS. It says nothing about a routing table that
drifted across two hundred generations, a peer's accumulated trust score, a
threshold tuned twenty times, or a retention index rebuilt against a policy
that has since changed. Those are accumulated statistics, and an accumulated
statistic has no inverse: reverting the component that wrote it does not
restore the world in which it was never written, and the next generation
inherits the drift as if it were measurement.

Of the seven layers the 512-541 wave would evolve, four are stateful this way
(routing, workflows, memory, device placement) and three are not (policies,
components, runtime). The guarantee that is supposed to make evolution safe
covers the three that are not the problem.

THE RULE
--------
A layer's state class is a property of the LAYER, not of the effects that
wrote it, and it is decided in two places that compose as a CEILING and a
CLAIM.

1. :data:`LAYERS` is a registry and owns a per-layer CEILING: the strongest
   class that layer's state may ever be claimed at. A layer that accumulates
   may not be claimed `revertible` by anyone.
2. A :class:`RollbackPlan` DECLARES a class per part of each layer. The
   declaration may sit at the ceiling or below it, never above.
3. A layer's aggregate class is the WEAKEST class among its declared parts, so
   a plan cannot make an accumulating layer look revertible by declaring only
   its revertible half.

Absence is not a class. A part with no declaration resolves to
:data:`UNDECLARED`, which is ordered BELOW `neither` and which no compensation
can lift: the only exit from it is to declare. `neither` is a stated class
with a stated escape (a declared and tested compensation); `UNDECLARED` is the
absence of a statement and has none. This is the shape
`lower._spawn_emission_surface` uses when it maps an uncapped emission method
to the unnameable `*` rather than to `set()`: the missing answer resolves to
the value nothing covers, not to the empty one.

CONSUMED, NOT DUPLICATED
------------------------
Two lanes answered this shape for one layer each, and this module is the
general rule they are special cases of.

* Item 518 (issue #1192) answers it for shadow promotion: a revert RESTORES
  one layer and COMPENSATES two. It declares a class per layer in the plan and
  the gate checks the declaration rather than assuming it. Its concern is a
  module ASSUMING a class it cannot know, and its own note records that its
  classes are "a default a plan may disagree with".
* Item 522 (issue #1196) answers it for the computer-use surface: the class is
  REGISTRY-OWNED rather than author-declared, because a classification an
  author can lower is one a careless author lowers. Its concern is an author
  RAISING a class (a click declared reversible).

They disagree about who declares, and the ceiling-and-claim rule is the
reconciliation: an author cannot raise a class past what the registry says the
layer can be, and a plan can still lower it for its own deployment. Neither
concern is dropped.

Item 522's five classes fold into the three by :func:`fold`. `reversible` is
`revertible`; `irreversible` and `unknown` are both `neither`, kept apart in
the REASON and joined in the DECISION exactly as item 522 joins them;
`confirm-required` is not a state class at all and is refused by name, because
it says who may authorise the step rather than what the step leaves behind.

FAILURE DIRECTION
-----------------
Fail-closed, with no default anywhere. An unaccounted layer refuses rather
than being read as untouched. An undeclared part refuses and cannot be bought
back with a compensation. A class above its layer's ceiling refuses. A
non-revertible part with no compensation refuses; a compensation that is not
recorded tested refuses. The one direction that would make this module
pointless is a layer whose class was never stated defaulting to `revertible`,
and that value is not reachable by omission anywhere in this file.

NO NEW GUARANTEE CODE
---------------------
Every refusal here is a named lowercase LINK, the discipline `revl.deploy`
uses, and none is a G-code. A promotion gate refusing a plan is not the
checker refusing a program, and item 523's generated tier matrix requires
every registered G-code to carry a reproducer under `examples/rejections/` or
an `ACKNOWLEDGED` entry in `tools/tier_guarantees.py`. This module registers
none and owes neither. :data:`LINKS` is asserted G-free by the test file.

"ROLLED BACK" IS NOT A WORD THIS MODULE USES
--------------------------------------------
:meth:`Rollback.render` prints one sentence per outcome and the phrase "rolled
back" appears in none of them, because it covers two different outcomes and
that is the thing the item asks to stop. The outcome vocabulary is deliberately
NOT the class vocabulary: a `compensatable` part whose compensation did not run
is `uncompensated`, which is item 522's word for the same fact.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "LAYERS", "STATE_CLASSES", "UNDECLARED", "UNTOUCHED", "LINKS",
    "Layer", "Part", "LayerDeclaration", "RollbackPlan", "Refusal",
    "Rollback", "PlanRefused",
    "weakest", "at_or_below", "fold", "classify", "check", "report",
    "UI_CLASSES", "SHADOW_PARTS",
]

MODULE_VERSION = 1

# ---------------------------------------------------------------------------
# the vocabulary
# ---------------------------------------------------------------------------

#: The item's three classes, ordered from weakest to strongest. The order is
#: the whole arithmetic of this module: a layer's aggregate is the weakest of
#: its parts, and a claim is admissible when it is at or below its ceiling.
#:
#: `neither`       no inverse and no path back. Refused without a compensation.
#: `compensatable` no inverse, but a declared path that makes the world
#:                 acceptable again (superseding a window, re-deriving an
#:                 index). It does NOT restore.
#: `revertible`    an inverse exists and replaying it restores the prior world.
STATE_CLASSES = ("neither", "compensatable", "revertible")

#: The absence of a declaration. Ordered below every class in
#: :data:`STATE_CLASSES` and reachable by no compensation: a part that nobody
#: classified is not a part that is hard to restore, it is a part nobody
#: looked at. Kept separate from `neither` for the same reason item 522 keeps
#: `unknown` separate from `irreversible`: the two refusals say different
#: true things, and only one of them has an exit.
UNDECLARED = "undeclared"

#: A layer-level declaration that this promotion does not write the layer at
#: all. Not a state class: it contributes nothing to an aggregate and appears
#: in its own list on the report. Stating it is required, because a layer
#: simply left out of a plan is an omission and an omission must not read as
#: an untouched layer.
UNTOUCHED = "untouched"

_ORDER = {UNDECLARED: -1}
_ORDER.update({name: i for i, name in enumerate(STATE_CLASSES)})


def weakest(classes: Sequence[str]) -> str:
    """The aggregate of a set of part classes: the WEAKEST of them.

    A layer is exactly as restorable as its least restorable part. This is
    what stops a plan from declaring only routing's revertible arm and
    reporting routing as restored while the accumulated preference that the
    item is actually about goes unmentioned."""
    if not classes:
        return UNDECLARED
    return min(classes, key=lambda c: _ORDER.get(c, -1))


def at_or_below(claim: str, ceiling: str) -> bool:
    """Whether `claim` is admissible under `ceiling`. Unknown tokens are not."""
    if claim not in _ORDER or ceiling not in _ORDER:
        return False
    return _ORDER[claim] <= _ORDER[ceiling]


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Layer:
    """One evolvable layer, with the strongest class it may be claimed at.

    `accumulates` is the item's own finding for this layer and `ceiling` is
    derived from it rather than stated twice: a layer that accumulates state
    may not be claimed `revertible` by any plan, so its ceiling is
    `compensatable`."""

    name: str
    accumulates: bool
    reason: str

    @property
    def ceiling(self) -> str:
        return "compensatable" if self.accumulates else "revertible"


#: The seven layers the 512-541 wave would evolve, with the four the item
#: names as stateful. The item's prose says the guarantee "covers only the
#: two that are not"; its own table lists three (policies, components,
#: runtime) and seven minus four is three. The table is taken.
LAYERS: tuple = (
    Layer("policies", False,
          "a bounded rule. The rule set is the whole of the state and "
          "re-declaring the prior set restores the prior world exactly."),
    Layer("routing", True,
          "accumulated preference. The table drifted across generations and "
          "reverting the component that wrote it does not restore the world "
          "in which it was never written."),
    Layer("components", False,
          "revl's own guarantee: a revertible effect carries its inverse and "
          "unloading replays the inverses in LIFO order."),
    Layer("workflows", True,
          "learned plan shape. The shape is a statistic over the plans that "
          "ran, and no inverse un-runs them."),
    Layer("memory", True,
          "index and retention state. A retention index rebuilt against a "
          "policy that has since changed cannot be rebuilt against the "
          "policy that is gone."),
    Layer("runtime", False,
          "an admitted capability is a declaration. Withdrawing the "
          "admission restores the prior authority."),
    Layer("device-placement", True,
          "accumulated placement history. The next generation inherits the "
          "drift as if it were measurement."),
)

_BY_NAME = {layer.name: layer for layer in LAYERS}

#: Item 518's three parts, and the layer each belongs to. Recorded here so the
#: two items share one registry instead of two: `route-arm` is a declaration
#: and restores, `agreement-ledger` was observed and cannot be unobserved,
#: `placement-history` is this item's own row. The CLASSES are item 518's
#: plan's to declare; only the part-to-layer assignment lives here.
SHADOW_PARTS = {
    "route-arm": "routing",
    "agreement-ledger": "routing",
    "placement-history": "device-placement",
}

#: Item 522's five computer-use classes folded into the three, with the reason
#: kept so the diagnostic does not lose what the five-way split buys.
#: `confirm-required` has no fold and is refused by :func:`fold`.
UI_CLASSES = {
    "reversible": ("revertible", "the step changes no state the target owns"),
    "compensatable": ("compensatable",
                      "an inverse exists and only the author can write it"),
    "irreversible": ("neither", "no inverse exists"),
    "unknown": ("neither", "revl cannot tell, and the missing answer is the "
                           "case the item exists for"),
}


def fold(vocabulary: str, token: str) -> tuple:
    """Fold a sibling item's class token into this item's three.

    Returns `(state_class, reason)`. `vocabulary` is `"ui"` (item 522's five)
    or `"state"` (this item's three, so a caller can normalise either).

    Item 522's `confirm-required` is refused rather than folded: it states who
    may authorise the step, not what the step leaves behind, and folding an
    authority class into a state class is how a vocabulary silently grows a
    fourth meaning. Item 522's own note says it is orthogonal to the three;
    this is that sentence made executable."""
    if vocabulary == "state":
        if token in _ORDER and token != UNDECLARED:
            return (token, "declared directly")
        raise PlanRefused(Refusal(
            CLASS_UNKNOWN,
            f"{token!r} is not one of {list(STATE_CLASSES)}."))
    if vocabulary != "ui":
        raise PlanRefused(Refusal(
            CLASS_UNKNOWN,
            f"no fold is defined for vocabulary {vocabulary!r}; the known "
            f"ones are 'state' and 'ui'."))
    if token == "confirm-required":
        raise PlanRefused(Refusal(
            CLASS_NOT_A_STATE_CLASS,
            "'confirm-required' says who may authorise a step, not what the "
            "step leaves behind. It is orthogonal to the three state classes "
            "and folding it into one would give the vocabulary a fourth "
            "meaning nothing reads. Declare the state class separately."))
    if token not in UI_CLASSES:
        raise PlanRefused(Refusal(
            CLASS_UNKNOWN,
            f"{token!r} is not one of item 522's classes "
            f"{sorted(UI_CLASSES) + ['confirm-required']}."))
    return UI_CLASSES[token]


# ---------------------------------------------------------------------------
# refusal links
# ---------------------------------------------------------------------------
#
# Named lowercase reasons so a caller branches on WHICH check refused instead
# of parsing a sentence. No G-code: see the module header.

PLAN_MALFORMED = "plan-malformed"
LAYER_UNKNOWN = "layer-unknown"
LAYER_UNACCOUNTED = "layer-unaccounted"
LAYER_UNDECLARED = "layer-undeclared"
CLASS_UNKNOWN = "class-unknown"
CLASS_NOT_A_STATE_CLASS = "class-not-a-state-class"
CLASS_ABOVE_CEILING = "class-above-ceiling"
STATE_NOT_RESTORABLE = "state-not-restorable"
COMPENSATION_UNTESTED = "compensation-untested"
COMPENSATION_ORPHANED = "compensation-orphaned"

#: Every link, so a caller can assert exhaustiveness and so the test file can
#: assert that none of them is a guarantee code.
LINKS = (
    PLAN_MALFORMED,
    LAYER_UNKNOWN,
    LAYER_UNACCOUNTED,
    LAYER_UNDECLARED,
    CLASS_UNKNOWN,
    CLASS_NOT_A_STATE_CLASS,
    CLASS_ABOVE_CEILING,
    STATE_NOT_RESTORABLE,
    COMPENSATION_UNTESTED,
    COMPENSATION_ORPHANED,
)

#: The outcome vocabulary of a rollback. Deliberately not the class
#: vocabulary: a class is what a part IS, an outcome is what a rollback DID,
#: and a `compensatable` part whose compensation did not run is
#: `uncompensated`. `uncompensated` is item 522's word for that fact and is
#: taken from it rather than renamed.
OUTCOMES = ("restored", "compensated", "uncompensated", "untouched")


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Refusal:
    """A named refusal. `link` is one of :data:`LINKS`."""

    link: str
    detail: str
    layer: Optional[str] = None
    part: Optional[str] = None

    def as_dict(self) -> dict:
        return {"link": self.link, "detail": self.detail,
                "layer": self.layer, "part": self.part}

    def __str__(self) -> str:  # pragma: no cover - convenience
        where = "/".join(x for x in (self.layer, self.part) if x)
        return f"{self.link}: {self.detail}" + (f" [{where}]" if where else "")


class PlanRefused(ValueError):
    """Raised by the strict entry points. Carries the :class:`Refusal`."""

    def __init__(self, refusal: Refusal):
        super().__init__(str(refusal))
        self.refusal = refusal


@dataclass(frozen=True)
class Part:
    """One declared part of a layer's state.

    `state` is a member of :data:`STATE_CLASSES`. `compensation` is `None`, or
    a mapping carrying at least `name` and a truthy `tested`."""

    name: str
    state: str
    compensation: Optional[Mapping[str, Any]] = None

    @property
    def compensation_name(self) -> Optional[str]:
        comp = self.compensation
        if isinstance(comp, Mapping):
            value = comp.get("name")
            return value if isinstance(value, str) and value else None
        return None

    @property
    def compensation_tested(self) -> bool:
        comp = self.compensation
        return bool(isinstance(comp, Mapping) and comp.get("tested"))


@dataclass(frozen=True)
class LayerDeclaration:
    """What a plan says about one layer.

    Either `untouched` is true and `parts` is empty, or `parts` is non-empty.
    Both, or neither, is :data:`PLAN_MALFORMED`: a layer that is written and
    not written is not a statement."""

    layer: str
    parts: tuple = ()
    untouched: bool = False

    def aggregate(self) -> str:
        return weakest([p.state for p in self.parts])


@dataclass(frozen=True)
class RollbackPlan:
    """A promotion's account of every evolvable layer.

    `declarations` must cover every layer in :data:`LAYERS`. A layer left out
    is :data:`LAYER_UNACCOUNTED` and is NOT read as untouched, because an
    omission is silence and silence is the fail-open direction this item
    exists to close."""

    promotion: str
    declarations: tuple = ()
    note: str = ""

    def by_layer(self) -> dict:
        return {d.layer: d for d in self.declarations}


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def classify(plan: RollbackPlan, layer: str) -> str:
    """The resolved aggregate class of one layer under `plan`.

    :data:`UNDECLARED` when the layer is absent or has an undeclared part,
    :data:`UNTOUCHED` when the plan says so."""
    decl = plan.by_layer().get(layer)
    if decl is None:
        return UNDECLARED
    if decl.untouched:
        return UNTOUCHED
    return decl.aggregate()


def _check_shape(plan: RollbackPlan) -> Optional[Refusal]:
    if not isinstance(plan, RollbackPlan):
        return Refusal(PLAN_MALFORMED, "plan is not a RollbackPlan.")
    if not isinstance(plan.promotion, str) or not plan.promotion:
        return Refusal(PLAN_MALFORMED, "plan names no promotion.")
    seen = set()
    for decl in plan.declarations:
        if not isinstance(decl, LayerDeclaration):
            return Refusal(PLAN_MALFORMED,
                           "a declaration is not a LayerDeclaration.")
        if decl.layer in seen:
            return Refusal(PLAN_MALFORMED,
                           f"layer {decl.layer!r} is declared twice.",
                           layer=decl.layer)
        seen.add(decl.layer)
        if decl.layer not in _BY_NAME:
            return Refusal(
                LAYER_UNKNOWN,
                f"{decl.layer!r} is not an evolvable layer. The registry is "
                f"closed: {[x.name for x in LAYERS]}.",
                layer=decl.layer)
        if decl.untouched and decl.parts:
            return Refusal(
                PLAN_MALFORMED,
                f"layer {decl.layer!r} is declared untouched and also "
                f"declares {len(decl.parts)} part(s). A layer that is written "
                f"and not written is not a statement.",
                layer=decl.layer)
        if not decl.untouched and not decl.parts:
            return Refusal(
                LAYER_UNDECLARED,
                f"layer {decl.layer!r} declares no part and is not declared "
                f"untouched. Absence is not a class: declare the parts this "
                f"promotion writes, or declare the layer untouched.",
                layer=decl.layer)
        part_names = set()
        for part in decl.parts:
            if not isinstance(part, Part) or not part.name:
                return Refusal(PLAN_MALFORMED,
                               f"layer {decl.layer!r} carries a part that is "
                               f"not a named Part.", layer=decl.layer)
            if part.name in part_names:
                return Refusal(PLAN_MALFORMED,
                               f"part {part.name!r} is declared twice.",
                               layer=decl.layer, part=part.name)
            part_names.add(part.name)
    return None


def _check_coverage(plan: RollbackPlan) -> Optional[Refusal]:
    declared = plan.by_layer()
    for layer in LAYERS:
        if layer.name not in declared:
            return Refusal(
                LAYER_UNACCOUNTED,
                f"the plan says nothing about layer {layer.name!r}. Every "
                f"evolvable layer must be accounted for, with its parts or "
                f"as untouched. An omitted layer is not an untouched layer, "
                f"and reading it as one is the fail-open direction: "
                f"{layer.reason}",
                layer=layer.name)
    return None


def _check_classes(plan: RollbackPlan) -> Optional[Refusal]:
    for decl in plan.declarations:
        for part in decl.parts:
            if part.state == UNDECLARED or part.state not in _ORDER:
                if part.state in (UNDECLARED, "", None):
                    return Refusal(
                        LAYER_UNDECLARED,
                        f"part {part.name!r} of layer {decl.layer!r} has no "
                        f"declared class. It resolves to {UNDECLARED!r}, "
                        f"which is ordered below every class and which no "
                        f"compensation lifts: a part nobody classified is not "
                        f"a part that is hard to restore. Declare one of "
                        f"{list(STATE_CLASSES)}.",
                        layer=decl.layer, part=part.name)
                return Refusal(
                    CLASS_UNKNOWN,
                    f"part {part.name!r} of layer {decl.layer!r} declares "
                    f"{part.state!r}, which is not one of "
                    f"{list(STATE_CLASSES)}.",
                    layer=decl.layer, part=part.name)
    return None


def _check_ceilings(plan: RollbackPlan) -> Optional[Refusal]:
    for decl in plan.declarations:
        if decl.untouched:
            continue
        layer = _BY_NAME[decl.layer]
        aggregate = decl.aggregate()
        if not at_or_below(aggregate, layer.ceiling):
            strongest = [p.name for p in decl.parts if p.state == aggregate]
            return Refusal(
                CLASS_ABOVE_CEILING,
                f"layer {decl.layer!r} resolves to {aggregate!r} from parts "
                f"{strongest}, and its ceiling is {layer.ceiling!r}. This "
                f"layer accumulates state, so no plan may claim it restores: "
                f"{layer.reason} Declaring only the parts that do have an "
                f"inverse is how a layer that drifted reports as restored.",
                layer=decl.layer)
    return None


def _check_compensations(plan: RollbackPlan) -> Optional[Refusal]:
    for decl in plan.declarations:
        for part in decl.parts:
            name = part.compensation_name
            if part.state == "revertible":
                if name:
                    return Refusal(
                        COMPENSATION_ORPHANED,
                        f"part {part.name!r} of layer {decl.layer!r} is "
                        f"declared revertible and also names the "
                        f"compensation {name!r}. A revertible part is "
                        f"restored by its inverse; a compensation parked on "
                        f"it is a path nothing runs and a reader will count "
                        f"it as cover that is not there.",
                        layer=decl.layer, part=part.name)
                continue
            if not name:
                return Refusal(
                    STATE_NOT_RESTORABLE,
                    f"part {part.name!r} of layer {decl.layer!r} is declared "
                    f"{part.state!r} and names no compensation. Reverting "
                    f"the promotion does not restore it, so a promotion "
                    f"without a declared compensation ends in residue the "
                    f"rollback cannot even name.",
                    layer=decl.layer, part=part.name)
            if not part.compensation_tested:
                return Refusal(
                    COMPENSATION_UNTESTED,
                    f"part {part.name!r} of layer {decl.layer!r} names the "
                    f"compensation {name!r} and does not record it as "
                    f"tested. An untested compensation is a claim that the "
                    f"rollback will work, which is the thing a test exists "
                    f"to support.",
                    layer=decl.layer, part=part.name)
    return None


#: The checks, in order. Shape first, then coverage, then the classes, then
#: the ceiling, then the compensations. The order is not cosmetic: a plan that
#: does not account for a layer is refused before any class it DID declare is
#: read, so a well-declared routing block cannot distract from a memory block
#: that is not there.
CHECKS = (_check_shape, _check_coverage, _check_classes, _check_ceilings,
          _check_compensations)


def check(plan: RollbackPlan) -> Optional[Refusal]:
    """The gate. `None` when the plan may be promoted, else the first
    :class:`Refusal`."""
    for probe in CHECKS:
        refusal = probe(plan)
        if refusal is not None:
            return refusal
    return None


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rollback:
    """What a rollback of `promotion` actually did, per layer.

    Four lists, because there are four outcomes and one word for them is the
    thing item 546 exists to stop. `fully_restored` is true only when
    `compensated` and `uncompensated` are both empty."""

    promotion: str
    restored: tuple = ()
    compensated: tuple = ()
    uncompensated: tuple = ()
    untouched: tuple = ()
    refusal: Optional[Refusal] = None
    version: int = MODULE_VERSION

    @property
    def fully_restored(self) -> bool:
        return not self.compensated and not self.uncompensated

    def as_dict(self) -> dict:
        return {
            "kind": "revl.layer_state.rollback",
            "version": self.version,
            "promotion": self.promotion,
            "restored": list(self.restored),
            "compensated": [dict(x) for x in self.compensated],
            "uncompensated": [dict(x) for x in self.uncompensated],
            "untouched": list(self.untouched),
            "fully_restored": self.fully_restored,
            "refusal": self.refusal.as_dict() if self.refusal else None,
        }

    def render(self) -> str:
        """One sentence per outcome. The phrase "rolled back" is not among
        them, and :data:`OUTCOMES` is the vocabulary a reader gets instead."""
        if self.refusal is not None:
            return (f"{self.promotion}: refused, {self.refusal.link}. "
                    f"{self.refusal.detail}")
        lines = []
        if self.restored:
            lines.append(
                f"Restored to the state before {self.promotion}: "
                + ", ".join(self.restored) + ".")
        if self.compensated:
            lines.append(
                "Not restored, compensated: "
                + ", ".join(f"{x['layer']} by {x['compensation']}"
                            for x in self.compensated)
                + ". The prior world is not back; these layers were made "
                  "acceptable again by a declared path.")
        if self.uncompensated:
            lines.append(
                "Neither restored nor compensated: "
                + ", ".join(f"{x['layer']} ({x['state']})"
                            for x in self.uncompensated)
                + ". This residue survives the revert.")
        if self.untouched:
            lines.append(
                "Not written by this promotion, so nothing to undo: "
                + ", ".join(self.untouched) + ".")
        if not lines:
            lines.append(f"{self.promotion}: no layer was accounted for.")
        return "\n".join(lines)


def report(plan: RollbackPlan, *,
           ran: Optional[Sequence[str]] = None) -> Rollback:
    """Which layers a rollback of `plan` RESTORED and which it COMPENSATED.

    `ran` is the set of compensation names that actually executed. It defaults
    to every declared compensation, which is the "the rollback completed"
    reading; passing a smaller set is how a partial rollback reports the rest
    as `uncompensated` rather than losing it. The outcome is therefore a
    measurement of what ran, not a restatement of the plan's classes.

    A refused plan produces a `Rollback` carrying the refusal and no lists:
    there is nothing to report about a promotion that may not happen."""
    refusal = check(plan)
    if refusal is not None:
        return Rollback(promotion=getattr(plan, "promotion", "") or "",
                        refusal=refusal)
    declared = {
        part.compensation_name
        for decl in plan.declarations for part in decl.parts
        if part.compensation_name
    }
    executed = declared if ran is None else {
        name for name in ran if name in declared}

    restored, compensated, uncompensated, untouched = [], [], [], []
    for layer in LAYERS:
        decl = plan.by_layer()[layer.name]
        if decl.untouched:
            untouched.append(layer.name)
            continue
        missed = [p for p in decl.parts
                  if p.state != "revertible"
                  and p.compensation_name not in executed]
        covered = [p for p in decl.parts
                   if p.state != "revertible"
                   and p.compensation_name in executed]
        if missed:
            uncompensated.append({
                "layer": layer.name,
                "state": weakest([p.state for p in missed]),
                "parts": [p.name for p in missed],
                "reason": layer.reason,
            })
        elif covered:
            compensated.append({
                "layer": layer.name,
                "state": weakest([p.state for p in covered]),
                "parts": [p.name for p in covered],
                "compensation": ", ".join(
                    sorted({p.compensation_name for p in covered})),
            })
        else:
            restored.append(layer.name)
    return Rollback(
        promotion=plan.promotion,
        restored=tuple(restored),
        compensated=tuple(compensated),
        uncompensated=tuple(uncompensated),
        untouched=tuple(untouched),
    )


# ---------------------------------------------------------------------------
# adapters, so a sibling item declares once
# ---------------------------------------------------------------------------


def from_shadow_layers(promotion: str,
                       layers: Mapping[str, str],
                       compensations: Mapping[str, Mapping[str, Any]],
                       *, untouched: Sequence[str] = ()) -> RollbackPlan:
    """Lift item 518's `(layers, compensations)` maps into a plan here.

    Item 518 declares three PARTS of two layers (`route-arm` and
    `agreement-ledger` under routing, `placement-history` under device
    placement). The remaining five layers are not written by a model-action
    promotion, and `untouched` is where the caller says so: this adapter will
    not infer it, because inferring it is the omission-reads-as-untouched
    shape the coverage check exists to refuse.

    Takes plain mappings rather than item 518's `ShadowPlan` class on purpose:
    that module is an open pull request and this one does not import it."""
    grouped: dict = {}
    for part_name, state in layers.items():
        layer = SHADOW_PARTS.get(part_name)
        if layer is None:
            raise PlanRefused(Refusal(
                LAYER_UNKNOWN,
                f"part {part_name!r} has no layer in SHADOW_PARTS "
                f"{sorted(SHADOW_PARTS)}.", part=part_name))
        grouped.setdefault(layer, []).append(
            Part(part_name, state, compensations.get(part_name)))
    for name in compensations:
        if name not in layers:
            raise PlanRefused(Refusal(
                COMPENSATION_ORPHANED,
                f"a compensation is declared for part {name!r}, which the "
                f"plan does not declare a class for.", part=name))
    decls = [LayerDeclaration(layer, tuple(parts))
             for layer, parts in grouped.items()]
    decls += [LayerDeclaration(name, (), untouched=True)
              for name in untouched]
    return RollbackPlan(promotion=promotion, declarations=tuple(decls))

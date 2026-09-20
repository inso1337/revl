"""The promotion barrier, stated once (roadmap item 543, issue #1222).

WHAT THIS MODULE IS FOR
-----------------------
Two promotion paths in this tree already implement the barrier issue #1222
asks for, and both implement it well:

  * `tools/evolution_controller.py` (item 520) walks
    `trigger -> propose -> admit -> authority` and RETURNS on the first
    unsatisfied precondition before a measured record is read;
  * `src/revl/shadow_promotion.py` (item 518) walks
    `plan -> route -> admit -> evidence -> policy -> authority -> state`
    and builds no `Tally` until every one of them is satisfied.

Neither of them is the item. The item is the RULE, and two independent
implementations that happen to agree are not a rule: nothing forces a third
promotion path to have the barrier, and nothing tells one of the two when the
other changes. That was measured rather than assumed. The two modules spell
their axes differently (`capability`/`capabilities`, `taint`/`origins`,
`realm`/`residence`), so an authority diff computed for one is not readable by
the other, and only `budget` and `retention` are spelled the same in both.

So this module states the barrier once:

  * :data:`AUTHORITY_AXES` is the canonical axis set and :data:`AXIS_ALIASES`
    maps every spelling in the tree onto it, so the two vocabularies compose;
  * :func:`authority_moved` is the fail-closed reading of a diff, shared;
  * :class:`PromotionPath` is what a promotion path declares about its own
    stage order, and :func:`check_path` refuses one whose authority stage is
    not strictly before every measured stage;
  * :func:`discover` sweeps the tree for modules that render a promote
    decision, so a path added tomorrow that nobody registers is found.

THE FAILURE DIRECTION
---------------------
Fail-closed, with one rule applied everywhere: an axis that is ABSENT from a
diff has MOVED. It has not been measured, and reading an unmeasured axis as an
empty one is the fail-open shape and is the whole bug. A promotion that
proceeds because nobody looked is indistinguishable, from the outside, from a
promotion that proceeded because somebody looked and found nothing; the
difference is exactly what the barrier exists to preserve.

The same rule governs :func:`check_path`. A path that declares no authority
stage is refused, not defaulted; a path that declares an axis this module does
not know is refused rather than dropped; and an axis a path cannot measure is
recorded as uncovered rather than quietly excluded from the set it claims.

NO NEW GUARANTEE CODE
---------------------
The refusals here are lowercase LINKS, the discipline `revl.deploy`,
`revl.model_evidence` and `revl.shadow-promotion` use. A rule refusing a
promotion path's SHAPE is not the checker refusing a program, and registering a
G-code would pull in item 523's generated tier matrix, which wants a reproducer
under `examples/rejections/` or an `ACKNOWLEDGED` entry in
`tools/tier_guarantees.py` for every code.

PUBLIC SURFACE
--------------
``AUTHORITY_AXES``          : the five canonical axes
``canonical_axis(name)``    : one spelling -> one axis, or None
``normalise_diff(diff)``    : a diff in any spelling -> canonical keys
``authority_moved(diff)``   : `(moved_axes, why)`, fail-closed
``PromotionPath``           : what a promotion path declares about itself
``check_path(path)``        : the ordering rule, as a refusal or None
``REGISTRY``                : every promotion path in this tree
``renders_promotion(src)``  : does this module source render a promote verdict
``discover(root)``          : modules that do, and are not in ``REGISTRY``
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass

BARRIER_KIND = "revl.promotion-barrier"


# ---------------------------------------------------------------------------
# the axes
# ---------------------------------------------------------------------------

#: The authority axes, canonically spelled. These are item 536's conditions
#: (issue #1206) read as a set rather than as prose: capability reach, taint
#: edges, resource budget, realm residence and retention deadline. They are
#: STATIC properties of a program, which is the whole argument for the barrier.
#: A canary on one percent of traffic measures the paths traffic happens to
#: take, and the rare path that widens reach is exactly the path it does not
#: exhibit.
AUTHORITY_AXES = ("budget", "capability", "realm", "retention", "taint")

#: Every spelling of an axis used anywhere in this tree, mapped onto the
#: canonical one. This table is the thing that holds the promotion paths
#: together: without it, `tools/evolution_controller.py`'s diff and
#: `src/revl/shadow_promotion.py`'s diff are two objects with five keys each
#: that agree on two of them, and each reads the other's three as UNMEASURED.
#: That direction is fail-closed and therefore safe, but it is not
#: composition: it means neither can be handed the other's evidence at all.
AXIS_ALIASES = {
    # tools/evolution_controller.py
    "capability": "capability",
    "taint": "taint",
    "budget": "budget",
    "realm": "realm",
    "retention": "retention",
    # src/revl/shadow_promotion.py
    "capabilities": "capability",
    "origins": "taint",
    "residence": "realm",
}


def canonical_axis(name) -> str | None:
    """The canonical axis one spelling names, or ``None`` if it names none.

    ``None`` is a refusal, never a default. A caller that maps an unknown axis
    onto some nearest neighbour has invented an authority the author did not
    write.
    """
    if not isinstance(name, str):
        return None
    return AXIS_ALIASES.get(name.strip().lower())


def normalise_diff(diff) -> tuple[dict, list]:
    """``(canonical_diff, unknown_keys)`` for a diff in any spelling.

    Unknown keys are RETURNED rather than dropped, because a diff carrying an
    axis this module does not know is a diff whose author was measuring
    something; silently discarding it is how a gate reports on four axes and
    calls it five.
    """
    if not isinstance(diff, dict):
        return {}, []
    out: dict = {}
    unknown: list = []
    for key, value in diff.items():
        axis = canonical_axis(key)
        if axis is None:
            unknown.append(key)
            continue
        out[axis] = value
    return out, sorted(unknown, key=str)


def authority_moved(diff, axes=AUTHORITY_AXES) -> tuple[list, str]:
    """``(moved_axes, why)``: the axes on which authority is not provably empty.

    The shared fail-closed reading, and the one both landed implementations
    already apply in their own vocabulary:

      * the diff is not a mapping at all      -> every axis moved
      * an axis is absent                     -> that axis moved (UNMEASURED,
                                                 which is not empty)
      * an axis is not a sequence of what
        widened                               -> that axis moved
      * an axis is a non-empty sequence       -> that axis moved

    Only an axis that is present AND is a sequence AND is empty is still.
    """
    canonical, unknown = normalise_diff(diff)
    if not isinstance(diff, dict):
        return list(axes), "the authority diff is not a mapping of axis to what widened"
    moved = []
    reasons = []
    for axis in axes:
        if axis not in canonical:
            moved.append(axis)
            reasons.append(f"{axis} is unmeasured")
            continue
        entries = canonical[axis]
        if not isinstance(entries, (list, tuple, set, frozenset)):
            moved.append(axis)
            reasons.append(f"{axis} is {type(entries).__name__}, not a sequence "
                           f"of what widened")
        elif entries:
            moved.append(axis)
    if unknown:
        reasons.append("unknown axes carried and not read: " + ", ".join(unknown))
    return moved, "; ".join(reasons)


# ---------------------------------------------------------------------------
# refusal links (lowercase, per `revl.deploy`; no G-code is registered here)
# ---------------------------------------------------------------------------

NO_AUTHORITY_STAGE = "authority-not-a-precondition"
AUTHORITY_AFTER_MEASUREMENT = "authority-after-measurement"
AUTHORITY_IS_MEASURED = "authority-read-as-a-measurement"
AXIS_UNKNOWN = "authority-axis-unknown"
AXIS_DROPPED = "authority-axis-dropped"
NO_MEASURED_STAGE = "no-measured-stage"
STAGE_ORDER_MALFORMED = "stage-order-malformed"
PATH_UNREGISTERED = "promotion-path-unregistered"


@dataclass(frozen=True)
class Refusal:
    """One named refusal, in `src/revl/shadow_promotion.py`'s shape."""

    link: str
    reason: str

    def as_dict(self) -> dict:
        return {"link": self.link, "reason": self.reason}


# ---------------------------------------------------------------------------
# what a promotion path declares about itself
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PromotionPath:
    """One promotion path's own account of its stage order.

    ``stages`` is the FULL order the path walks, ``measured`` names the subset
    whose evidence is a measurement, and ``authority`` names the stage that
    reads the authority diff. ``covers`` is the axes this path actually
    measures and ``uncovered`` the axes it does not; their union must be
    :data:`AUTHORITY_AXES`, so an axis cannot leave a path's account by being
    forgotten.
    """

    module: str
    stages: tuple
    measured: tuple
    authority: str
    covers: tuple
    uncovered: tuple = ()
    note: str = ""

    def index(self, stage: str) -> int:
        return self.stages.index(stage)


def check_path(path: PromotionPath) -> Refusal | None:
    """The ordering rule. ``None`` when the path has the barrier.

    This is the general form of what issue #1222 asks for, and it refuses the
    exact shape the issue names as wrong: a lifecycle that lists the capability
    and reachability diff "as one stage among those nine", beside the measured
    ones rather than in front of them.

      1. the path declares an authority stage at all;
      2. that stage is in the walk;
      3. it is not itself a measured stage;
      4. the path has at least one measured stage (a path with none has
         nothing to be a barrier in front of, and declaring one would be a
         check that cannot fail);
      5. the authority stage is strictly before EVERY measured stage.

    Rule 5 is the barrier. Rules 1 and 4 are why it is not vacuous.
    """
    if not isinstance(path.stages, tuple) or not path.stages:
        return Refusal(STAGE_ORDER_MALFORMED,
                       f"{path.module} declares no stage order, so there is no "
                       f"position for a barrier to be in")
    if len(set(path.stages)) != len(path.stages):
        return Refusal(STAGE_ORDER_MALFORMED,
                       f"{path.module} names a stage twice, so 'before' is not "
                       f"a well-defined relation over its walk")
    if not path.authority:
        return Refusal(NO_AUTHORITY_STAGE,
                       f"{path.module} declares no authority stage. The "
                       f"capability, taint, budget, realm and retention diff "
                       f"is a PRECONDITION of promotion (item 543, issue "
                       f"#1222); a path that names none has no barrier, and "
                       f"the absence is refused rather than defaulted")
    if path.authority not in path.stages:
        return Refusal(NO_AUTHORITY_STAGE,
                       f"{path.module} names {path.authority!r} as its "
                       f"authority stage and does not walk it")
    if path.authority in path.measured:
        return Refusal(AUTHORITY_IS_MEASURED,
                       f"{path.module} reads {path.authority!r} as a "
                       f"measurement. Capability reach and taint edges are "
                       f"static properties of the program; a measured reading "
                       f"of them is a sample, and a sample is the wrong KIND "
                       f"of evidence about the path that widens reach")
    missing = [s for s in path.measured if s not in path.stages]
    if missing:
        return Refusal(STAGE_ORDER_MALFORMED,
                       f"{path.module} calls {', '.join(missing)} measured and "
                       f"does not walk them")
    if not path.measured:
        return Refusal(NO_MEASURED_STAGE,
                       f"{path.module} declares no measured stage, so this "
                       f"check would pass on it whatever its order was. A "
                       f"promotion path with nothing to hold back does not "
                       f"need a barrier and must not claim one")
    here = path.index(path.authority)
    late = sorted(s for s in path.measured if path.index(s) < here)
    if late:
        return Refusal(
            AUTHORITY_AFTER_MEASUREMENT,
            f"{path.module} reads {', '.join(late)} before its "
            f"{path.authority!r} stage. A canary on one percent of traffic "
            f"measures latency, refusal rate and answer quality on the paths "
            f"that traffic happens to take, while capability reach and taint "
            f"edges are static properties of the program, so the rare path "
            f"that widens them is exactly the path the sample does not "
            f"exhibit. The authority diff gates ENTRY to those stages; it is "
            f"not weighed beside their output (item 543, issue #1222)")
    return None


def check_axes(path: PromotionPath) -> Refusal | None:
    """Every axis is accounted for: covered, or named as not covered.

    A path measuring four axes and calling it five is the same fail-open shape
    one level down, so the union of ``covers`` and ``uncovered`` must be the
    canonical set exactly.
    """
    for axis in tuple(path.covers) + tuple(path.uncovered):
        if canonical_axis(axis) is None:
            return Refusal(AXIS_UNKNOWN,
                           f"{path.module} names the axis {axis!r}, which is "
                           f"not one of {list(AUTHORITY_AXES)}")
    accounted = {canonical_axis(a) for a in path.covers} | {
        canonical_axis(a) for a in path.uncovered}
    dropped = sorted(set(AUTHORITY_AXES) - accounted)
    if dropped:
        return Refusal(AXIS_DROPPED,
                       f"{path.module} accounts for neither covering nor "
                       f"failing to cover: {', '.join(dropped)}. An axis that "
                       f"leaves a path's account by being forgotten is the "
                       f"unmeasured-read-as-empty shape at the level of the "
                       f"path rather than of one diff")
    overlap = sorted({canonical_axis(a) for a in path.covers}
                     & {canonical_axis(a) for a in path.uncovered})
    if overlap:
        return Refusal(AXIS_DROPPED,
                       f"{path.module} claims to both cover and not cover: "
                       f"{', '.join(overlap)}")
    return None


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------

#: `tools/evolution_controller.py` (item 520, PR #1241, merged). Its own
#: `PRECONDITIONS` and `MEASURED` tuples are the source of truth; the binding
#: test reads them off the module and compares, so moving `authority` after
#: `shadow` there reds here.
_CONTROLLER = PromotionPath(
    module="tools/evolution_controller.py",
    stages=("trigger", "propose", "admit", "authority",
            "shadow", "canary", "observe"),
    measured=("shadow", "canary", "observe"),
    authority="authority",
    covers=("capability", "taint", "budget", "realm", "retention"),
    note="reads a declared diff and recomputes the changed-file set from git; "
         "refuses by name on AUTHORITY_WIDENED before any measured record",
)

#: `src/revl/shadow_promotion.py` (item 518, PR #1250). Not on `main` at the
#: time this landed, so the binding test skips with a stated reason when the
#: module is absent and compares when it is present.
_SHADOW = PromotionPath(
    module="src/revl/shadow_promotion.py",
    stages=("route", "evidence", "policy", "authority", "state",
            "accumulate"),
    measured=("accumulate",),
    authority="authority",
    covers=("budget", "capability", "taint", "realm", "retention"),
    note="the metric block is never carried into `Pair`, so the measured "
         "evidence is structurally absent left of the barrier",
)

#: `src/revl/mcp/canary.py` (item 59). The promotion path that was on `main`
#: with no authority precondition at all: its recommendation turned on a replay
#: comparison of the slice provider's own recorded steps, which is a reading of
#: the paths the slice takes. It now runs the tree's own authority-drift gate
#: (`src/revl/audit_diff.py`) before the recommendation is formed.
_CANARY = PromotionPath(
    module="src/revl/mcp/canary.py",
    stages=("slice", "admit", "authority", "divergence", "revert"),
    measured=("divergence",),
    authority="authority",
    covers=("capability", "taint", "realm", "budget"),
    uncovered=("retention",),
    note="the authority diff is `audit_diff`'s, over the G8 boundary surface "
         "of the whole composition; retention rows are reported by the audit "
         "and have no differ, so the axis is named as not covered rather than "
         "counted as clean",
)

REGISTRY = (_CANARY, _CONTROLLER, _SHADOW)

BY_MODULE = {p.module: p for p in REGISTRY}


# ---------------------------------------------------------------------------
# the sweep: is there a promotion path nobody registered
# ---------------------------------------------------------------------------

#: Where a promotion path can live. Both halves are swept, because the two
#: landed implementations live in different ones.
SEARCH_ROOTS = ("src/revl", "tools")

#: This module states the rule, so it carries the token the rule looks for
#: (`renders_promotion`'s own comparison). It is exempt by name and for that
#: one reason. The exemption is a literal path, never a pattern: a pattern is
#: how a real promotion path ends up exempt by accident.
SWEEP_EXEMPT = ("src/revl/promotion_barrier.py",)


def renders_promotion(source: str) -> bool:
    """Does this module source render a promote verdict.

    The rule is deliberately syntactic and deliberately narrow: a module
    renders a promotion when it contains a string constant whose value is
    exactly ``promote``, in any case. That is what a module that decides
    promotions has and what one that merely discusses them does not. Measured
    over this tree, it selects `src/revl/mcp/canary.py` and
    `tools/evolution_controller.py` and nothing else, and it selects
    `src/revl/shadow_promotion.py` when that lands.

    A module that decided promotions without ever spelling the word would be
    missed. That is stated rather than hidden: the sweep is a ratchet against
    the ordinary case of a fourth path being added, not a proof that none can
    exist.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value.strip().lower() == "promote":
            return True
    return False


def discover(root, roots=SEARCH_ROOTS) -> list:
    """Module paths under `root` that render a promotion and are unregistered.

    Returns repository-relative paths with forward slashes, sorted. An empty
    list is the property: every promotion path in the tree has declared its
    stage order and is held to :func:`check_path`.
    """
    found = []
    for base in roots:
        top = os.path.join(str(root), *base.split("/"))
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", ".git")]
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, str(root)).replace(os.sep, "/")
                if rel in BY_MODULE or rel in SWEEP_EXEMPT:
                    continue
                try:
                    with open(full, encoding="utf-8") as handle:
                        source = handle.read()
                except OSError:
                    continue
                if renders_promotion(source):
                    found.append(rel)
    return sorted(found)


def unregistered_refusal(paths) -> Refusal | None:
    """The sweep's refusal, for a caller that wants the reason rendered."""
    if not paths:
        return None
    return Refusal(
        PATH_UNREGISTERED,
        "these modules render a promotion verdict and declare no stage order, "
        "so nothing holds them to the authority precondition: "
        + ", ".join(paths)
        + ". Add a `PromotionPath` to `src/revl/promotion_barrier.py`'s "
          "REGISTRY naming the stage that reads the authority diff and the "
          "measured stages it gates entry to (item 543, issue #1222)")

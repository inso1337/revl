"""Shadow routing: the agreement accumulator and the promotion predicate
(roadmap item 518, issue #1192).

Design note: `docs/design/540-shadow-promotion.md`.

WHAT IS MISSING, AND WHAT THIS IS
---------------------------------
Moving an action from one model to a successor has no gate. `revl canary`
(`revl.mcp.canary`) already compares two recorded worlds and attributes the
first differing step to an exact `(component, realm)`, which is the evidence a
promotion decision needs. What it does not do is accumulate that evidence per
action class and turn the accumulation into a verdict.

This module is that half, and only that half. It does not schedule a shadow,
does not serve traffic, and does not run a model. It is a REDUCER over sealed
model-decision records (item 517, `revl.model_evidence`) plus a declared plan,
and its whole contribution is WHICH evidence is allowed to matter and in WHAT
ORDER.

THE RECORDS IT READS
--------------------
One shadow observation is a PAIR of item 517 records for the same crossing:
the incumbent's decision and the candidate's. Item 517's body is keyed by
`(component, step_index)`, which is exactly `revl.wal.model_decisions`' key, so
pairing invents no second correlation and no parallel log is written. The
records are the evidence; this module adds a tally over them.

Verification is delegated to `revl.model_evidence.verify`. That module arrives
with item 517 and may not be importable yet, so the verifier is a PARAMETER
with a fail-closed default: when the verifier cannot be resolved, no record is
verified and the evidence precondition refuses. An unresolvable verifier is a
refusal, never a skipped check.

THE BARRIER, AND WHY IT IS STRUCTURAL
-------------------------------------
Issue #1222 (item 543) is binding here. A one percent shadow measures latency,
refusal rate and answer quality ON THE PATHS THAT TRAFFIC HAPPENS TO TAKE,
while capability reach and taint edges are STATIC properties of the program.
The rare path that widens reach is precisely the path a small sample does not
exhibit. So the authority diff is a PRECONDITION and not a stage: no amount of
SLO evidence may buy a non-empty one.

`decide()` enforces that by control flow, the shape PR #1241 uses for the
evolution controller one level up. It walks the preconditions in order and
RETURNS on the first unsatisfied one, before any tally exists. The forbidden
comparison cannot be written because the values are not in scope.

This module goes one step further than a return-early, because it can. The
barrier is also a DATA barrier, in two parts:

1. **Metric evidence does not cross it at all.** An :class:`Observation` may
   carry an `slo` block (latency, cost, refusal rate). `_admit()` does not copy
   it into the :class:`Pair` objects the rest of the module works on, so no
   precondition and no tally can read a metric even by mistake. `slo` is read
   through a tripwire property, and the verdict reports `slo_reads`, a COUNT
   taken from the observations rather than a boolean this module asserts about
   itself. Every path in this module leaves it at zero. A test asserts that on
   the promote path, on the refuse path and on the revert path.

2. **A precondition may read the QUESTION; only the tally may read the
   ANSWER.** :class:`Pair` exposes the crossing, the two roles, the policy
   digest, the two placement digests and the prompt binding as public members.
   The members that say what the model SAID (`candidates`, `chosen`,
   `outcome`) are private to the pair and are read only by :func:`accumulate`,
   which runs after the precondition walk has returned nothing. A precondition
   cannot compute agreement from what it is given.

WHAT AGREEMENT IS, AND WHAT IT IS NOT
-------------------------------------
Agreement is a COMPARISON of two recorded answers, not a threshold on a
counter. Item 496's rationale, restated: a metric cannot attribute a
divergence to the component that caused it, and this module's whole output
when a pair disagrees is the attribution.

Two records agree when :data:`AGREEMENT_MEMBERS` match: the digest of the
completion that was taken, and the outcome. `fallback_depth`, `sampling` and
`model_digest` are deliberately NOT compared. They are how the answer was
reached and by what weights, and a successor reaching the same answer by a
different path is the case a promotion exists to allow. `residence` is not
compared either, for the same reason and because the two roles differ by
construction.

Two records naming the same completion is not the whole of agreement. When a
shadow observation also carries the two RECORDED WORLDS (`replay.Timeline`
objects in `revl.mcp.canary`'s own format), :func:`accumulate` compares them
with `canary.compare_timelines` and a difference there is a divergence too,
reported under :data:`WORLD_COMPARISON`. Item 496 is why: it measured a
generation that named the same completion, recorded a different world, and was
certified equivalent and promoted. Nothing about that comparison is restated
here. It is called, so its key stays item 496's and the attribution stays the
one it fixed. When the comparator cannot be imported at all, a pair carrying
two worlds DIVERGES rather than agreeing, because "the comparison did not run"
must not read as "the worlds matched".

A pair whose prompt bindings do not match, or whose binding was SUPPRESSED on
either side, is not comparable and refuses (`BINDING_INCOMPARABLE`,
`BINDING_SUPPRESSED`). Two answers to possibly-different questions cannot
witness agreement. Item 517's suppression is taint-driven and legitimate; what
must not happen is a suppressed binding counting as a silent agreement. This
is the fail-open shape the repository has measured eleven times and it is
refused here rather than admitted as a pass.

THRESHOLD AND REVERT ARE DIFFERENT RULES ON DIFFERENT SIDES OF THE PROMOTION
---------------------------------------------------------------------------
The item asks for both "promote on a stated threshold" and "revert on the
first attributed divergence". Those are not the same rule and the module keeps
them apart by the plan's `live` flag:

* **Before promotion** (`live=False`) the candidate answers nothing. A
  divergence lowers the accumulated agreement, and the verdict is `PROMOTE`
  only when the sample reaches `min_observations` AND the ratio reaches
  `threshold`.
* **After promotion** (`live=True`) the candidate's answer is the one in use.
  The FIRST attributed divergence is a `REVERT`, whatever the ratio is, and
  the verdict names the crossing and the member that differed.

FAILURE DIRECTION
-----------------
Fail-closed, with no third value and no default. Absent evidence refuses;
an unresolvable verifier refuses; an authority axis that was not measured
refuses (`DIFF_UNMEASURED`) rather than being read as empty; a layer whose
state class was not declared refuses (`LAYER_UNDECLARED`); every vocabulary is
closed. A promotion that proceeds when evidence is MISSING rather than failing
is the shape this module exists to not be.

WHAT A REVERT RESTORES, AND WHAT IT ONLY COMPENSATES
----------------------------------------------------
Issue #1225 (item 546): revertible effects give no inverse for accumulated
state, and a promotion that has been live accumulates exactly that. So a plan
declares a state class from :data:`STATE_CLASSES` for every layer in
:data:`PROMOTION_LAYERS`, a layer that is not `revertible` may not be promoted
without a compensation that is both declared AND tested, and a `REVERT`
verdict reports `restored` and `compensated` as two separate lists. The word
"rolled back" is not used for either, and :func:`render` prints two sentences
rather than one.

The three layers a model-action promotion touches, and their classes as this
module judges them:

* `route-arm` — the declared arm of the action's `route model` block. A
  declaration, so re-declaring it RESTORES. Revertible.
* `agreement-ledger` — the accumulated tally itself. Append-only observation:
  reverting the arm does not unobserve the window. At best compensatable, by
  marking the window superseded.
* `placement-history` — issue #1225's own row. Accumulated placement
  preference, no inverse.

The plan declares them; this module does not assume them. A plan that claims
`route-arm` is `neither` is refused for a missing compensation exactly like
any other, because the point is that the declaration is checked, not that this
module knows better than the declarer.

NO NEW GUARANTEE CODE
---------------------
The refusals here are named LINKS, the discipline `revl.deploy` and
`revl.model_evidence` use, and deliberately not G-codes. A verifier refusing a
record is not the checker refusing a program. Registering a G-code would pull
in item 523's generated tier matrix, which requires a reproducer under
`examples/rejections/` or an `ACKNOWLEDGED` entry in `tools/tier_guarantees.py`
for every code; the routing refusals that ARE checker refusals already exist
and already carry `G-MODEL-PLACE` (`revl.model_route`).

PUBLIC SURFACE
--------------
``ShadowPlan``            — the declaration a promotion is judged against
``Observation``           — one shadow pair, plus the metric block nobody reads
``Pair`` / ``Tally``      — the admitted pair and the accumulated agreement
``Promotion``             — the verdict
``admit(plan, obs, ...)`` — records -> pairs, fail-closed
``accumulate(pairs)``     — pairs -> ``Tally`` (the post-barrier reduction)
``refused(plan, ...)``    : a REFUSE verdict for a CALLER's own precondition
``decide(plan, obs, ...)``— the whole gate
``render(promotion)``     — one human-readable report
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------

#: The self-identifying tag, the kind/version idea `revl.mcp.canary` and
#: `revl.model_evidence` both use.
PROMOTION_KIND = "revl.shadow-promotion"

#: MAJOR.MINOR, additive within a MAJOR.
PROMOTION_VERSION = "1.0"

#: The terminal shapes, and there are exactly three. Not a score: under any
#: weighting with positive weight on the measured side, a candidate with a
#: marginally non-empty authority diff and perfect agreement clears a
#: threshold, and a gate satisfiable in a way its own preconditions refuse is
#: a defect rather than a design choice.
#:
#: `REVERT` is reported only when the candidate had gone live. A refusal
#: before promotion has nothing to undo, and reporting a revert there would
#: claim an undo that never ran.
PROMOTE = "PROMOTE"
REVERT = "REVERT"
REFUSE = "REFUSE"

DECISIONS = (PROMOTE, REFUSE, REVERT)

#: The members two records must match on to be said to AGREE. The digest of
#: the completion that was taken, and the outcome. See the module header for
#: what is excluded and why.
AGREEMENT_MEMBERS = ("chosen_digest", "outcome")

#: The name a divergence carries when the two RECORDED WORLDS differ although
#: both records named the same completion. This is not a member of a record: it
#: is the verdict of `revl.mcp.canary.compare_timelines` over the two
#: `replay.Timeline` objects a shadow observation may carry, which is item
#: 496's own comparison and its own attribution, reused rather than restated.
#:
#: A pair AGREES when the two :data:`AGREEMENT_MEMBERS` match AND, when both
#: worlds were recorded, the two worlds compare identical step-for-step. Item
#: 496 measured why the second half is load-bearing: two generations can name
#: the same completion and still record different worlds, and the direction of
#: that miss is fail-open, because "no divergence" is the promoting branch.
WORLD_COMPARISON = "recorded-world"

#: The axes an authority diff is measured on. EVERY one must be present in a
#: plan's `authority_diff`; a missing axis is `DIFF_UNMEASURED` and not an
#: assumed empty. The names are the four issue #1222 and PR #1241 enumerate
#: (capability reach, taint edges, resource ceilings, retention) plus
#: `residence`, which is this item's own: a promotion from an `on_device` role
#: to an `off_device` one moves the prompt off the device, which is an
#: authority change whatever the agreement says.
AUTHORITY_AXES = ("budget", "capabilities", "origins", "residence",
                  "retention")

#: Issue #1225's three classes, for the state a layer accumulates while the
#: promotion runs.
#:
#: `revertible`    — an inverse exists and restores the prior world.
#: `compensatable` — no inverse, but a declared path that makes the world
#:                   acceptable again (superseding a window, re-deriving an
#:                   index). It does not restore.
#: `neither`       — no inverse and no compensation. Refused without one.
STATE_CLASSES = ("compensatable", "neither", "revertible")

#: The layers a model-action promotion accumulates state in. Every one must
#: carry a declared class in the plan. See the module header for why these
#: three and what they are.
PROMOTION_LAYERS = ("agreement-ledger", "placement-history", "route-arm")

#: Item 517's prompt-binding modes that a shadow pair may be compared across.
#: `suppressed` is absent on purpose: a suppressed binding cannot witness that
#: the two worlds were asked the same question.
COMPARABLE_BINDING_MODES = ("content-addressed", "salted-within-run")

#: The members a `slo` block may carry. Enumerated so a reader can see exactly
#: what this module declines to read, not because anything here reads them.
SLO_MEMBERS = ("cost", "latency_ms", "refusal_rate", "tokens")


# ---------------------------------------------------------------------------
# refusal links
# ---------------------------------------------------------------------------
#
# Named reasons, so a caller branches on WHICH check refused instead of
# parsing a sentence. Grouped by the phase they can fire in, because the phase
# is the thing issue #1222 cares about.

# -- the plan itself is not a plan -------------------------------------------
PLAN_MALFORMED = "plan-malformed"
PLAN_VOCABULARY = "plan-vocabulary"

# -- preconditions: the route table (item 512, docs/design/531 section 9) -----
ROUTE_UNKNOWN_ACTION = "route-unknown-action"
ROUTE_NOT_ROUTABLE = "route-not-routable"
ROLES_IDENTICAL = "roles-identical"

# -- preconditions: the evidence ---------------------------------------------
EVIDENCE_MISSING = "evidence-missing"
EVIDENCE_UNVERIFIED = "evidence-unverified"
EVIDENCE_UNVERIFIABLE = "evidence-unverifiable"
CROSSING_MISMATCHED = "crossing-mismatched"
ROLE_MISPLACED = "role-misplaced"
BINDING_SUPPRESSED = "binding-suppressed"
BINDING_INCOMPARABLE = "binding-incomparable"

# -- preconditions: the rule in force ----------------------------------------
POLICY_DRIFT = "policy-drift"
PLACEMENT_UNPINNED = "placement-unpinned"

# -- preconditions: the authority diff (issue #1222) -------------------------
DIFF_UNMEASURED = "diff-unmeasured"
AUTHORITY_WIDENED = "authority-widened"

# -- preconditions: revertibility (issue #1225) ------------------------------
LAYER_UNDECLARED = "layer-undeclared"
STATE_NOT_RESTORABLE = "state-not-restorable"
COMPENSATION_UNTESTED = "compensation-untested"

# -- measured, and reachable only past the barrier ---------------------------
SAMPLE_TOO_SMALL = "sample-too-small"
AGREEMENT_BELOW_THRESHOLD = "agreement-below-threshold"
DIVERGENCE_ATTRIBUTED = "divergence-attributed"

#: Every link, for a caller that wants to assert exhaustiveness.
LINKS = (
    PLAN_MALFORMED, PLAN_VOCABULARY,
    ROUTE_UNKNOWN_ACTION, ROUTE_NOT_ROUTABLE, ROLES_IDENTICAL,
    EVIDENCE_MISSING, EVIDENCE_UNVERIFIED, EVIDENCE_UNVERIFIABLE,
    CROSSING_MISMATCHED, ROLE_MISPLACED, BINDING_SUPPRESSED,
    BINDING_INCOMPARABLE,
    POLICY_DRIFT, PLACEMENT_UNPINNED,
    DIFF_UNMEASURED, AUTHORITY_WIDENED,
    LAYER_UNDECLARED, STATE_NOT_RESTORABLE, COMPENSATION_UNTESTED,
    SAMPLE_TOO_SMALL, AGREEMENT_BELOW_THRESHOLD, DIVERGENCE_ATTRIBUTED,
)

#: The links a PRECONDITION can produce. Nothing in this set may be reached
#: after the barrier, and nothing outside it may be reached before. The test
#: file asserts the partition against the two phase functions.
PRECONDITION_LINKS = (
    PLAN_MALFORMED, PLAN_VOCABULARY,
    ROUTE_UNKNOWN_ACTION, ROUTE_NOT_ROUTABLE, ROLES_IDENTICAL,
    EVIDENCE_MISSING, EVIDENCE_UNVERIFIED, EVIDENCE_UNVERIFIABLE,
    CROSSING_MISMATCHED, ROLE_MISPLACED, BINDING_SUPPRESSED,
    BINDING_INCOMPARABLE,
    POLICY_DRIFT, PLACEMENT_UNPINNED,
    DIFF_UNMEASURED, AUTHORITY_WIDENED,
    LAYER_UNDECLARED, STATE_NOT_RESTORABLE, COMPENSATION_UNTESTED,
)

#: The links only the MEASURED phase can produce.
MEASURED_LINKS = (SAMPLE_TOO_SMALL, AGREEMENT_BELOW_THRESHOLD,
                  DIVERGENCE_ATTRIBUTED)


class PromotionRefused(ValueError):
    """Raised by the strict entry points. Carries the link a
    :class:`Promotion` would."""

    def __init__(self, link: str, reason: str):
        super().__init__(f"{link}: {reason}")
        self.link = link
        self.reason = reason


@dataclass(frozen=True)
class Refusal:
    """One named refusal. ``where`` is the crossing it is attributed to, when
    the refusal has one."""

    link: str
    reason: str
    where: Optional[tuple] = None

    def as_dict(self) -> dict:
        return {"link": self.link, "reason": self.reason,
                "where": list(self.where) if self.where else None}


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShadowPlan:
    """The declaration a promotion is judged against.

    Everything here is DECLARED. Nothing is observed from a host, and nothing
    is defaulted: an absent member is a refusal, because a default is how a
    gate ends up passing on evidence that was never supplied.

    ``route_table`` is `revl.model_route.check()`'s return value verbatim,
    ``{component: {action: {origin: {role, residence}}}}``. Binding it by value
    is the point of `docs/design/531-model-placement.md` section 9: a promotion
    moves an action between roles its OWN block already names, so this is not a
    second placement channel and the table is read rather than re-derived.

    ``authority_diff`` maps every axis in :data:`AUTHORITY_AXES` to the
    sequence of things that WIDENED. An empty sequence is "nothing widened on
    this axis". A missing axis is `DIFF_UNMEASURED`.

    ``layers`` maps every layer in :data:`PROMOTION_LAYERS` to a class from
    :data:`STATE_CLASSES`; ``compensations`` maps a layer to
    ``{"name": str, "tested": bool}``.

    ``live`` is whether the candidate is already answering. It selects which of
    the two rules in the module header applies.
    """

    component: str
    action: str
    incumbent_role: str
    candidate_role: str
    route_table: Mapping[str, Mapping[str, Mapping[str, Mapping[str, str]]]]
    authority_diff: Mapping[str, Sequence[str]]
    layers: Mapping[str, str]
    threshold: float
    min_observations: int
    compensations: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict)
    live: bool = False

    @property
    def action_class(self) -> tuple:
        """The key agreement accumulates under: one component, one action.

        Item 517's record is keyed by `(component, step_index)` and does NOT
        name the action, so the ACTION half is declared here and the step half
        comes from the records. `docs/design/540-shadow-promotion.md` section 7
        records that as the one correlation this slice does not derive."""
        return (self.component, self.action)


# ---------------------------------------------------------------------------
# the observation, and the metric tripwire
# ---------------------------------------------------------------------------

class Observation:
    """One shadow observation: the incumbent's sealed record, the candidate's,
    and an optional block of measured SLO evidence.

    ``slo`` is a property rather than a field, and reading it increments
    :attr:`slo_reads`. Nothing in this module reads it. The counter exists so
    that the claim "no metric is consulted on the promotion path" is something
    a test MEASURES on a real run rather than something this docstring
    asserts, and so that a future caller that starts reading it shows up in the
    verdict instead of silently changing what the gate means.

    ``realm`` is the realm the shadow was served in. It is DECLARED by the
    scheduler (`revl.shadow_routing`) and not read off a record: item 517's
    body is a closed vocabulary keyed by `(component, step_index)` and names
    no realm. It is part of the QUESTION, so it is carried onto the
    :class:`Pair` in public and a divergence reports it, which is what makes
    the attribution the `(component, realm)` pair `revl canary` attributes to.

    ``incumbent_world`` and ``candidate_world`` are the two RECORDED WORLDS,
    as `replay.Timeline` objects in `revl.mcp.canary`'s own format. They are
    optional because a record-only window is still a window; when both are
    present :func:`accumulate` compares them with `canary.compare_timelines`
    and a difference is a divergence even when both records named the same
    completion.
    """

    __slots__ = ("incumbent", "candidate", "_slo", "slo_reads", "realm",
                 "incumbent_world", "candidate_world")

    def __init__(self, incumbent: Mapping[str, Any],
                 candidate: Mapping[str, Any],
                 slo: Optional[Mapping[str, Any]] = None,
                 realm: Optional[str] = None,
                 incumbent_world: Any = None,
                 candidate_world: Any = None):
        self.incumbent = incumbent
        self.candidate = candidate
        self._slo = dict(slo) if isinstance(slo, Mapping) else slo
        self.slo_reads = 0
        self.realm = realm
        self.incumbent_world = incumbent_world
        self.candidate_world = candidate_world

    @property
    def slo(self) -> Optional[Mapping[str, Any]]:
        self.slo_reads += 1
        return self._slo

    @property
    def has_slo(self) -> bool:
        """Whether a metric block was supplied, WITHOUT reading it.

        A caller and a test both need to say "this observation carried perfect
        SLO evidence and the gate refused anyway", and asking that question
        must not itself trip the counter the claim rests on."""
        return self._slo is not None

    @property
    def has_world(self) -> bool:
        """Whether BOTH recorded worlds were supplied. One world is not a
        comparison, so a pair with one is compared on its records alone."""
        return (self.incumbent_world is not None
                and self.candidate_world is not None)


@dataclass(frozen=True)
class Pair:
    """One admitted shadow pair.

    The public members are the QUESTION: which crossing, in which realm,
    which two roles, under which policy, on which two host profiles, bound to
    which input. A precondition reads these.

    The ANSWER lives in ``_incumbent``, ``_candidate`` and the two recorded
    worlds, and is read only by :func:`accumulate`, which runs after the
    precondition walk. A precondition is handed a tuple of these and cannot
    compute agreement from it. ``realm`` is on the public side because it says
    WHERE the shadow was served, not what it answered; it is the second half
    of the `(component, realm)` attribution `revl canary` reports.

    No member of an :class:`Observation`'s ``slo`` block is carried here. The
    metric evidence does not cross the barrier at all, which is a stronger
    statement than not consulting it.
    """

    crossing: tuple
    incumbent_role: str
    candidate_role: str
    policy_digest: Optional[str]
    incumbent_placement: Optional[str]
    candidate_placement: Optional[str]
    binding_mode: Optional[str]
    binding_value: Optional[str]
    _incumbent: Mapping[str, Any]
    _candidate: Mapping[str, Any]
    realm: Optional[str] = None
    _incumbent_world: Any = None
    _candidate_world: Any = None

    def _answer(self, record: Mapping[str, Any]) -> dict:
        """The two :data:`AGREEMENT_MEMBERS`, read off one record.

        ``chosen_digest`` is ``candidates[chosen]`` for a validated decision
        and ``None`` for any other outcome, which item 517's own
        ``_check_candidates`` guarantees is the only consistent shape: a
        non-validated record may not name a chosen index."""
        outcome = record.get("outcome")
        chosen = record.get("chosen")
        candidates = record.get("candidates")
        digest = None
        if isinstance(candidates, (list, tuple)) and isinstance(chosen, int) \
                and not isinstance(chosen, bool) and 0 <= chosen < len(candidates):
            digest = candidates[chosen]
        return {"chosen_digest": digest, "outcome": outcome}

    @property
    def incumbent_answer(self) -> dict:
        return self._answer(self._incumbent)

    @property
    def candidate_answer(self) -> dict:
        return self._answer(self._candidate)


# ---------------------------------------------------------------------------
# verification: delegated, and fail-closed when it cannot be
# ---------------------------------------------------------------------------

def _resolve_verifier() -> Optional[Callable]:
    """`revl.model_evidence.verify`, or ``None``.

    Item 517 lands that module; this one must be usable before it does and
    must not pretend to verify when it cannot. Resolution is lazy and the
    failure is a refusal at the call site, not a silent pass."""
    try:
        from .model_evidence import verify  # noqa: PLC0415 - optional, item 517
    except Exception:  # noqa: BLE001 - absence is the case being handled
        return None
    return verify


def _verified(record: Any, key: Optional[bytes],
              verifier: Optional[Callable]) -> Optional[Refusal]:
    """``None`` when the record verifies, a :class:`Refusal` otherwise."""
    if verifier is None:
        return Refusal(
            EVIDENCE_UNVERIFIABLE,
            "no model-decision verifier is available, so no record can be "
            "said to be evidence; item 517 (revl.model_evidence) supplies it, "
            "and its absence refuses rather than admitting an unchecked record")
    if key is None:
        return Refusal(
            EVIDENCE_UNVERIFIABLE,
            "a verifier was supplied with no key, so the MAC over the record "
            "cannot be recomputed")
    try:
        verdict = verifier(record, key)
    except Exception as error:  # noqa: BLE001 - a raising verifier is a refusal
        return Refusal(EVIDENCE_UNVERIFIED,
                       f"the verifier raised on this record: {error}")
    ok = getattr(verdict, "ok", None)
    if ok is None:
        ok = bool(verdict)
    if not ok:
        link = getattr(verdict, "link", "") or "unnamed"
        reason = getattr(verdict, "reason", "") or "no reason given"
        return Refusal(EVIDENCE_UNVERIFIED,
                       f"the record did not verify ({link}): {reason}")
    return None


# ---------------------------------------------------------------------------
# admission: observations -> pairs
# ---------------------------------------------------------------------------

def _crossing(record: Mapping[str, Any]) -> tuple:
    """``(component, step_index)``.

    `revl.model_evidence.crossing_key` when it is importable, so the two
    cannot drift, and the same two members read directly when it is not."""
    try:
        from .model_evidence import crossing_key  # noqa: PLC0415 - item 517
    except Exception:  # noqa: BLE001
        return (record.get("component"), record.get("step_index"))
    return crossing_key(record)


def _binding(record: Mapping[str, Any]) -> tuple:
    binding = record.get("prompt_binding")
    if not isinstance(binding, Mapping):
        return (None, None)
    return (binding.get("mode"), binding.get("value"))


def admit(plan: ShadowPlan, observations: Sequence[Observation], *,
          key: Optional[bytes] = None,
          verifier: Optional[Callable] = None,
          _resolve: Callable = _resolve_verifier) -> tuple:
    """``(pairs, refusal)``. Turn observations into :class:`Pair` objects, or
    name the first thing wrong with them.

    Fail-closed at every step, and deliberately does NOT read the answer
    members. What it checks is that a pair is EVIDENCE: both records verify,
    they describe the same crossing, they carry the plan's two roles the right
    way round, and they were asked the same question."""
    if not isinstance(observations, (list, tuple)) or not observations:
        return ((), Refusal(
            EVIDENCE_MISSING,
            "a promotion needs accumulated shadow evidence and none was "
            "supplied; an empty window is the absence of evidence, not "
            "evidence of agreement"))

    verifier = verifier if verifier is not None else _resolve()
    pairs = []
    for index, obs in enumerate(observations):
        if not isinstance(getattr(obs, "incumbent", None), Mapping) \
                or not isinstance(getattr(obs, "candidate", None), Mapping):
            return ((), Refusal(
                EVIDENCE_MISSING,
                f"observation {index} does not carry two model-decision "
                f"records"))
        for side, record in (("incumbent", obs.incumbent),
                             ("candidate", obs.candidate)):
            refusal = _verified(record, key, verifier)
            if refusal is not None:
                return ((), Refusal(
                    refusal.link,
                    f"observation {index} ({side} side): {refusal.reason}",
                    _crossing(record)))

        left, right = _crossing(obs.incumbent), _crossing(obs.candidate)
        if left != right:
            return ((), Refusal(
                CROSSING_MISMATCHED,
                f"observation {index} pairs {left} with {right}; a shadow "
                f"pair is two answers to ONE crossing, and pairing two "
                f"different ones measures nothing",
                left))
        if left[0] != plan.component:
            return ((), Refusal(
                CROSSING_MISMATCHED,
                f"observation {index} is a crossing of {left[0]!r}, which is "
                f"not the plan's component {plan.component!r}",
                left))

        roles = (obs.incumbent.get("role"), obs.candidate.get("role"))
        if roles != (plan.incumbent_role, plan.candidate_role):
            return ((), Refusal(
                ROLE_MISPLACED,
                f"observation {index} records roles {roles!r}, and the plan "
                f"promotes {plan.incumbent_role!r} -> {plan.candidate_role!r}; "
                f"a pair recorded the other way round would count the "
                f"incumbent's answer as the successor's",
                left))

        modes = (_binding(obs.incumbent)[0], _binding(obs.candidate)[0])
        for mode in modes:
            if mode == "suppressed":
                return ((), Refusal(
                    BINDING_SUPPRESSED,
                    f"observation {index} has a suppressed prompt binding, so "
                    f"nothing witnesses that the two worlds were asked the "
                    f"same question; a suppressed binding is a legitimate "
                    f"record and an unusable comparison, and counting it as "
                    f"agreement is the fail-open shape",
                    left))
            if mode not in COMPARABLE_BINDING_MODES:
                return ((), Refusal(
                    BINDING_INCOMPARABLE,
                    f"observation {index} carries prompt binding mode "
                    f"{mode!r}, which is outside "
                    f"{list(COMPARABLE_BINDING_MODES)}",
                    left))
        values = (_binding(obs.incumbent)[1], _binding(obs.candidate)[1])
        if modes[0] != modes[1] or values[0] != values[1]:
            return ((), Refusal(
                BINDING_INCOMPARABLE,
                f"observation {index} binds the two sides to different "
                f"inputs ({modes[0]}/{values[0]!r} against "
                f"{modes[1]}/{values[1]!r}); agreement between answers to two "
                f"questions is not agreement",
                left))

        pairs.append(Pair(
            crossing=left,
            incumbent_role=roles[0],
            candidate_role=roles[1],
            policy_digest=obs.incumbent.get("policy_digest"),
            incumbent_placement=obs.incumbent.get("placement_digest"),
            candidate_placement=obs.candidate.get("placement_digest"),
            binding_mode=modes[0],
            binding_value=values[0],
            _incumbent=obs.incumbent,
            _candidate=obs.candidate,
            realm=getattr(obs, "realm", None),
            _incumbent_world=getattr(obs, "incumbent_world", None),
            _candidate_world=getattr(obs, "candidate_world", None),
        ))
    return (tuple(pairs), None)


# ---------------------------------------------------------------------------
# the preconditions
# ---------------------------------------------------------------------------
#
# Each takes the plan and the admitted pairs and returns a Refusal or None.
# None of them can see a tally, because none exists yet, and none of them can
# see a metric, because `admit` did not carry one across.

def _check_plan(plan: ShadowPlan) -> Optional[Refusal]:
    if not isinstance(plan, ShadowPlan):
        return Refusal(PLAN_MALFORMED, f"{plan!r} is not a ShadowPlan")
    for name in ("component", "action", "incumbent_role", "candidate_role"):
        value = getattr(plan, name)
        if not isinstance(value, str) or not value:
            return Refusal(PLAN_MALFORMED,
                           f"plan.{name} is not a name ({value!r})")
    if not isinstance(plan.threshold, (int, float)) \
            or isinstance(plan.threshold, bool) \
            or not 0.0 < float(plan.threshold) <= 1.0:
        return Refusal(
            PLAN_MALFORMED,
            f"plan.threshold {plan.threshold!r} is not in (0, 1]; a threshold "
            f"of zero promotes on no agreement and one above one can never be "
            f"reached, and neither is a stated threshold")
    if not isinstance(plan.min_observations, int) \
            or isinstance(plan.min_observations, bool) \
            or plan.min_observations < 1:
        return Refusal(
            PLAN_MALFORMED,
            f"plan.min_observations {plan.min_observations!r} is not a "
            f"positive count; a promotion on zero observations is a promotion "
            f"on no evidence")
    if not isinstance(plan.route_table, Mapping):
        return Refusal(PLAN_MALFORMED,
                       "plan.route_table is not model_route.check()'s mapping")
    for name in ("authority_diff", "layers", "compensations"):
        if not isinstance(getattr(plan, name), Mapping):
            return Refusal(PLAN_MALFORMED, f"plan.{name} is not a mapping")
    for layer, state in plan.layers.items():
        if state not in STATE_CLASSES:
            return Refusal(
                PLAN_VOCABULARY,
                f"layer {layer!r} declares state class {state!r}, which is "
                f"outside {list(STATE_CLASSES)}")
    return None


def _precondition_route(plan: ShadowPlan,
                        pairs: Sequence[Pair]) -> Optional[Refusal]:
    """Both roles must be routable by the action's OWN block.

    `docs/design/531-model-placement.md` section 9, on item 518: "both roles
    must be routable by the action's own block for the shadow to be admissible
    at all, which is a property of the route table". This is that gate, reading
    `model_route.check()`'s table rather than re-deriving a placement."""
    if plan.incumbent_role == plan.candidate_role:
        return Refusal(
            ROLES_IDENTICAL,
            f"the plan promotes {plan.incumbent_role!r} to itself, which "
            f"moves nothing and would record a shadow that never ran")
    actions = plan.route_table.get(plan.component)
    if not isinstance(actions, Mapping) or plan.action not in actions:
        known = ", ".join(sorted(actions)) if isinstance(actions, Mapping) \
            else "none"
        return Refusal(
            ROUTE_UNKNOWN_ACTION,
            f"`route model on {plan.action}` is not declared by "
            f"{plan.component}; a promotion needs the action's own block to "
            f"name both roles, and an action with no block has no placement "
            f"to move between. {plan.component} routes: {known}")
    arms = actions[plan.action]
    routable = {arm.get("role") for arm in arms.values()
                if isinstance(arm, Mapping)}
    for which, role in (("incumbent", plan.incumbent_role),
                        ("candidate", plan.candidate_role)):
        if role not in routable:
            named = ", ".join(sorted(r for r in routable if r)) or "none"
            return Refusal(
                ROUTE_NOT_ROUTABLE,
                f"the {which} role {role!r} is not named by any arm of "
                f"`route model on {plan.action}` in {plan.component}. A "
                f"promotion moves an action between roles its own block "
                f"already routes to; promoting to a role the block does not "
                f"name would be a second placement channel beside item 512's "
                f"(docs/design/531-model-placement.md section 9). That block "
                f"names: {named}")
    return None


def _precondition_evidence(plan: ShadowPlan,
                           pairs: Sequence[Pair]) -> Optional[Refusal]:
    """There is evidence, and it is about this promotion.

    `admit` has already refused an unverifiable record, a mismatched crossing
    and an incomparable binding. What is left here is the shape of the WINDOW:
    a window with no pairs, and a window that repeats one crossing, which
    would let a single observation carry a whole promotion."""
    if not pairs:
        return Refusal(
            EVIDENCE_MISSING,
            "no shadow pair survived admission, so the promotion would rest "
            "on nothing")
    seen: dict = {}
    for pair in pairs:
        if pair.crossing in seen:
            return Refusal(
                CROSSING_MISMATCHED,
                f"crossing {pair.crossing} appears twice in the window; a "
                f"crossing is one completion, so a repeat is the same "
                f"observation counted twice and inflates the sample",
                pair.crossing)
        seen[pair.crossing] = pair
    return None


def _precondition_policy(plan: ShadowPlan,
                         pairs: Sequence[Pair]) -> Optional[Refusal]:
    """The rule in force, and the two host profiles, are pinned across the
    window.

    A window accumulated under two different policies is two windows. The
    placement digest is pinned PER SIDE and not across sides: two roles are
    two placements by construction, and requiring them equal would refuse
    every real promotion."""
    policy = pairs[0].policy_digest
    if not policy:
        return Refusal(
            POLICY_DRIFT,
            "the window's records carry no policy digest, so nothing says "
            "which rule the agreement was accumulated under",
            pairs[0].crossing)
    for pair in pairs:
        if pair.policy_digest != policy:
            return Refusal(
                POLICY_DRIFT,
                f"crossing {pair.crossing} was recorded under policy "
                f"{pair.policy_digest!r} and the window opened under "
                f"{policy!r}; agreement accumulated across a policy change is "
                f"two windows reported as one",
                pair.crossing)
    for side, first in (("incumbent", pairs[0].incumbent_placement),
                        ("candidate", pairs[0].candidate_placement)):
        if not first:
            return Refusal(
                PLACEMENT_UNPINNED,
                f"the {side} side carries no placement digest, so the host "
                f"profile the answers came from is unstated; item 515 makes "
                f"one model at two quantisation points two placements, and "
                f"an unpinned profile means the window may be a mixture",
                pairs[0].crossing)
    for pair in pairs:
        for side, value, first in (
                ("incumbent", pair.incumbent_placement,
                 pairs[0].incumbent_placement),
                ("candidate", pair.candidate_placement,
                 pairs[0].candidate_placement)):
            if value != first:
                return Refusal(
                    PLACEMENT_UNPINNED,
                    f"the {side} side's placement digest changed during the "
                    f"window ({first!r} -> {value!r} at crossing "
                    f"{pair.crossing}); the accumulated agreement is then "
                    f"about no single placement",
                    pair.crossing)
    return None


def _precondition_authority(plan: ShadowPlan,
                            pairs: Sequence[Pair]) -> Optional[Refusal]:
    """The authority diff must be MEASURED on every axis and EMPTY on all of
    them.

    Issue #1222, item 543. This is the barrier and it sits here, in the
    precondition walk, for a reason that is about sampling and not about
    ordering taste: capability reach and taint edges are static properties of
    the program, and a shadow serving a fraction of traffic exhibits only the
    paths that traffic took. The path that widens reach is exactly the one a
    small sample does not take, so no quantity of measured agreement, latency
    or cost is the right KIND of evidence about it.

    Two codes rather than one generic refusal, for the reason PR #1241 gives:
    a reader needs to know WHICH authority moved, and an axis that was never
    measured is a different failure from an axis that widened."""
    for axis in AUTHORITY_AXES:
        if axis not in plan.authority_diff:
            return Refusal(
                DIFF_UNMEASURED,
                f"the authority diff does not measure {axis!r}; an unmeasured "
                f"axis is refused rather than read as empty, because reading "
                f"it as empty is how a gate passes a widening nobody looked "
                f"for. Axes: {list(AUTHORITY_AXES)}")
        entries = plan.authority_diff[axis]
        if not isinstance(entries, (list, tuple, set, frozenset)):
            return Refusal(
                DIFF_UNMEASURED,
                f"the authority diff for {axis!r} is {entries!r}, which is "
                f"not a sequence of what widened")
    widened = {axis: sorted(str(e) for e in plan.authority_diff[axis])
               for axis in AUTHORITY_AXES if plan.authority_diff[axis]}
    if widened:
        detail = "; ".join(f"{axis}: {', '.join(items)}"
                           for axis, items in sorted(widened.items()))
        return Refusal(
            AUTHORITY_WIDENED,
            f"the promotion from {plan.incumbent_role!r} to "
            f"{plan.candidate_role!r} widens authority and no measurement can "
            f"buy that ({detail}). Capability reach and taint edges are "
            f"static properties; a sampled shadow measures the paths traffic "
            f"took, so agreement is not evidence about the path that widens "
            f"reach (issue #1222)")
    return None


def _precondition_state(plan: ShadowPlan,
                        pairs: Sequence[Pair]) -> Optional[Refusal]:
    """Every layer declares a state class, and a layer that is not revertible
    may not be promoted without a declared AND tested compensation.

    Issue #1225, item 546. A promotion that has been live has accumulated
    state, and a revertible effect gives no inverse for it. The exit clause
    this implements is the second bullet: "a layer whose state is neither may
    not be promoted without an explicitly declared and tested compensation
    path". It is applied to `compensatable` as well, because a compensation
    that exists and has never been run is the same absence of evidence with a
    better name."""
    for layer in PROMOTION_LAYERS:
        if layer not in plan.layers:
            return Refusal(
                LAYER_UNDECLARED,
                f"layer {layer!r} declares no state class; a promotion whose "
                f"accumulated state is undeclared cannot say what a revert "
                f"would restore. Layers: {list(PROMOTION_LAYERS)}")
    for layer in PROMOTION_LAYERS:
        state = plan.layers[layer]
        if state == "revertible":
            continue
        compensation = plan.compensations.get(layer)
        if not isinstance(compensation, Mapping) or not compensation.get("name"):
            return Refusal(
                STATE_NOT_RESTORABLE,
                f"layer {layer!r} is declared {state!r} and names no "
                f"compensation path. Reverting the promotion does not restore "
                f"the world in which this layer was never written, so a "
                f"promotion without a declared compensation would end in a "
                f"revert that reports an undo it cannot perform (issue #1225)")
        if not compensation.get("tested"):
            return Refusal(
                COMPENSATION_UNTESTED,
                f"layer {layer!r} names the compensation "
                f"{compensation.get('name')!r} and does not record it as "
                f"tested; an untested compensation is a declaration that the "
                f"revert will work, which is the claim the test exists to "
                f"support")
    return None


#: The one precondition that needs no evidence at all. It is a property of the
#: DECLARATION: `docs/design/531-model-placement.md` section 9 says both roles
#: must be routable by the action's own block "for the shadow to be admissible
#: at all", so it is checked before a single record is read. A promotion to a
#: role the block does not name is refused without ever asking what the shadow
#: measured.
PLAN_PRECONDITIONS = (
    ("route", _precondition_route),
)

#: The precondition walk over admitted evidence, IN ORDER. `decide` returns on
#: the first one that refuses: the evidence must be evidence about this
#: promotion, the rule must be pinned, the authority must not have moved, and
#: the state must be restorable. Only then is there a question a measurement
#: can answer.
PRECONDITIONS = (
    ("evidence", _precondition_evidence),
    ("policy", _precondition_policy),
    ("authority", _precondition_authority),
    ("state", _precondition_state),
)

#: Every stage, in the order `decide` walks them. `_verdict` fills the ones a
#: refusal never reached, so the artifact shows where the walk stopped.
STAGES = (tuple(name for name, _ in PLAN_PRECONDITIONS)
          + tuple(name for name, _ in PRECONDITIONS))


# ---------------------------------------------------------------------------
# past the barrier: the accumulator
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Divergence:
    """One pair whose two answers differ, attributed.

    ``member`` is the member of the record that differed, or
    :data:`WORLD_COMPARISON` when the records agreed and the two recorded
    worlds did not. ``realm`` is the realm the shadow was served in, so the
    attribution is the `(component, realm)` pair `revl canary` reports.

    When it is the world that diverged, ``at_step`` is the index of the first
    differing step and ``at_field`` is which of item 496's compared fields
    told the two apart. ``at_field`` matters for the case item 496 was filed
    about: a relocated step has the same kind and the same label on both
    sides, so a report that printed only those two would print the two sides
    identically and say nothing."""

    crossing: tuple
    member: str
    incumbent: Any
    candidate: Any
    realm: Optional[str] = None
    at_step: Optional[int] = None
    at_field: Optional[str] = None

    @property
    def attribution(self) -> tuple:
        """``(component, realm)``, the pair item 496's comparison names.

        The realm is ``None`` on a window whose scheduler did not declare one,
        and the component half is always present because it is the crossing
        key's own first member."""
        return (self.crossing[0], self.realm)

    def as_dict(self) -> dict:
        return {"crossing": list(self.crossing), "member": self.member,
                "incumbent": self.incumbent, "candidate": self.candidate,
                "realm": self.realm, "at_step": self.at_step,
                "at_field": self.at_field,
                "attribution": list(self.attribution)}

    def describe(self) -> str:
        where = f"{self.crossing[0]} step {self.crossing[1]}"
        if self.realm is not None:
            where = f"{self.crossing[0]} in realm `{self.realm}` step " \
                    f"{self.crossing[1]}"
        if self.member == WORLD_COMPARISON and self.at_step is not None:
            return (f"{where}: the recorded worlds differ at replay step "
                    f"{self.at_step} ({self.incumbent} -> {self.candidate})")
        return f"{where}: {self.member} {self.incumbent!r} -> {self.candidate!r}"


@dataclass(frozen=True)
class Tally:
    """Accumulated agreement for one action class.

    ``agreement`` is `agreed / paired`, a ratio over a COMPARISON. It is not a
    metric and there is no metric anywhere in this object: every field is a
    count of replay-comparison outcomes or the attribution of one."""

    action_class: tuple
    paired: int
    agreed: int
    diverged: int
    divergences: tuple

    @property
    def agreement(self) -> float:
        return (self.agreed / self.paired) if self.paired else 0.0

    @property
    def first_divergence(self) -> Optional[Divergence]:
        return self.divergences[0] if self.divergences else None

    def as_dict(self) -> dict:
        return {"action_class": list(self.action_class), "paired": self.paired,
                "agreed": self.agreed, "diverged": self.diverged,
                "agreement": self.agreement,
                "divergences": [d.as_dict() for d in self.divergences]}


def _resolve_comparator() -> Optional[Callable]:
    """`revl.mcp.canary.compare_timelines`, or ``None``.

    Resolved lazily for the same reason the verifier is: this module must be
    importable without dragging the MCP surface in, and an unavailable
    comparator must not turn into a skipped comparison. The call site treats
    ``None`` as a DIVERGENCE rather than as agreement, because "the comparison
    did not run" and "the two worlds matched" are the two branches item 496
    measured the cost of confusing."""
    try:
        from .mcp.canary import compare_timelines  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - absence is the case being handled
        return None
    return compare_timelines


def _world_divergence(pair: Pair,
                      compare: Optional[Callable]) -> Optional[Divergence]:
    """Compare the pair's two recorded worlds, or ``None`` when it carries
    fewer than two.

    The comparison is `revl.mcp.canary.compare_timelines` verbatim. That
    function walks the two `replay.Timeline` step lists in lock-step and
    reports the first index whose key differs, where the key is item 496's
    `(kind, label, undo, compensate, slot)`, the `slot` being the half of a
    step's provenance that is behaviour rather than a name. Nothing about that
    comparison is restated here; it is called."""
    left, right = pair._incumbent_world, pair._candidate_world
    if left is None or right is None:
        return None
    if compare is None:
        return Divergence(
            pair.crossing, WORLD_COMPARISON,
            "comparator unavailable", "comparator unavailable",
            realm=pair.realm)
    verdict = compare(left, right)
    if not verdict.get("diverged"):
        return None
    field = verdict.get("field")
    baseline = verdict.get("baseline") or {}
    candidate = verdict.get("candidate") or {}
    return Divergence(
        pair.crossing, WORLD_COMPARISON,
        _step_name(baseline, field), _step_name(candidate, field),
        realm=pair.realm, at_step=verdict.get("atIndex"), at_field=field)


def _step_name(step: Mapping[str, Any], field: Optional[str] = None) -> str:
    """A step as one string, for a divergence's two sides.

    ``absent`` when the generation had no step at that index, which is
    canary's length-mismatch shape. The discriminating field is appended
    because item 496's whole finding is a pair of steps that agree on `kind`
    and `label` and differ on `slot`: rendering only the first two would print
    both sides of that divergence identically."""
    if not step:
        return "absent"
    rendered = f"{step.get('kind')} {step.get('label')}"
    if field and field not in ("length", "kind", "label") and field in step:
        rendered += f" [{field}={step[field]}]"
    return rendered


def accumulate(action_class: tuple, pairs: Sequence[Pair], *,
               compare: Optional[Callable] = None,
               _resolve: Callable = _resolve_comparator) -> Tally:
    """Reduce admitted pairs to one :class:`Tally`.

    This is the ONLY function in the module that reads what a model said. It
    is reached only after the precondition walk in :func:`decide` has returned
    nothing, and the object it produces does not exist before that point.

    A pair is compared twice, and disagreeing on either is a divergence:

    1. on the members of :data:`AGREEMENT_MEMBERS`, which is what the
       decision said;
    2. on the two RECORDED WORLDS, when both were recorded, which is what
       the answer then did, compared by
       `revl.mcp.canary.compare_timelines`.

    The second is not a refinement of the first. Item 496 is the measurement
    that says so: two generations can name the same completion and record
    different worlds, and the fail-open direction is exactly that miss,
    because "no divergence" is the branch that promotes.

    Either way the divergence carries the crossing and the realm, so the
    attribution is to an exact `(component, realm)` and an exact step or
    member. That pair-of-names is the whole output when a shadow disagrees,
    and it is what a threshold on a counter cannot produce."""
    comparator = compare if compare is not None else _resolve()
    agreed = 0
    divergences = []
    for pair in pairs:
        left, right = pair.incumbent_answer, pair.candidate_answer
        difference = None
        for member in AGREEMENT_MEMBERS:
            if left.get(member) != right.get(member):
                difference = Divergence(pair.crossing, member,
                                        left.get(member), right.get(member),
                                        realm=pair.realm)
                break
        if difference is None:
            difference = _world_divergence(pair, comparator)
        if difference is None:
            agreed += 1
        else:
            divergences.append(difference)
    return Tally(action_class=action_class, paired=len(pairs), agreed=agreed,
                 diverged=len(divergences), divergences=tuple(divergences))


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Promotion:
    """The gate's answer.

    ``preconditions`` records every precondition and whether it was satisfied,
    reached or not reached, so the artifact itself shows where the walk
    stopped. ``measurements_read`` is false on every refusal produced by the
    precondition walk, and it is derived from whether a :class:`Tally` was
    built rather than set by hand.

    ``slo_reads`` is the number of times the supplied observations' metric
    blocks were read during this decision. It is counted on the observations
    and is zero on every path through this module."""

    kind: str
    version: str
    decision: str
    action_class: tuple
    incumbent_role: str
    candidate_role: str
    threshold: float
    refusal: Optional[Refusal]
    preconditions: tuple
    tally: Optional[Tally]
    restoration: Optional[dict]
    slo_reads: int
    slo_supplied: int

    @property
    def measurements_read(self) -> bool:
        return self.tally is not None

    @property
    def promoted(self) -> bool:
        return self.decision == PROMOTE

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "version": self.version,
            "decision": self.decision,
            "action_class": list(self.action_class),
            "incumbent_role": self.incumbent_role,
            "candidate_role": self.candidate_role,
            "threshold": self.threshold,
            "refusal": self.refusal.as_dict() if self.refusal else None,
            "preconditions": [dict(p) for p in self.preconditions],
            "tally": self.tally.as_dict() if self.tally else None,
            "measurements_read": self.measurements_read,
            "restoration": self.restoration,
            "slo_reads": self.slo_reads,
            "slo_supplied": self.slo_supplied,
        }


def _restoration(plan: ShadowPlan) -> dict:
    """Which layers a revert RESTORES and which it only COMPENSATES.

    Issue #1225's third exit bullet. The two lists are separate and the word
    "rolled back" appears in neither, because it covers two different
    outcomes and that is the thing the issue asks to stop.

    The `neither` bucket is empty by construction on any plan that reached a
    promotion, because `_precondition_state` refused a non-revertible layer
    with no tested compensation. It is reported anyway: the report should be
    readable without knowing which precondition ran."""
    restored, compensated, neither = [], [], []
    for layer in PROMOTION_LAYERS:
        state = plan.layers.get(layer)
        compensation = plan.compensations.get(layer)
        name = compensation.get("name") if isinstance(compensation, Mapping) \
            else None
        if state == "revertible":
            restored.append(layer)
        elif name:
            compensated.append({"layer": layer, "state": state,
                                "compensation": name})
        else:
            neither.append({"layer": layer, "state": state})
    return {"restored": restored, "compensated": compensated,
            "neither": neither,
            "fully_restored": not compensated and not neither}


def _verdict(plan: ShadowPlan, decision: str, refusal: Optional[Refusal],
             trail: Sequence[dict], tally: Optional[Tally],
             restoration: Optional[dict],
             observations: Sequence[Observation]) -> Promotion:
    reads = sum(getattr(o, "slo_reads", 0) for o in observations)
    supplied = sum(1 for o in observations if getattr(o, "has_slo", False))
    seen = {entry["stage"] for entry in trail}
    full = list(trail) + [
        {"stage": name, "status": "not reached"}
        for name in STAGES if name not in seen]
    return Promotion(
        kind=PROMOTION_KIND, version=PROMOTION_VERSION, decision=decision,
        action_class=plan.action_class if isinstance(plan, ShadowPlan)
        else (None, None),
        incumbent_role=getattr(plan, "incumbent_role", None),
        candidate_role=getattr(plan, "candidate_role", None),
        threshold=getattr(plan, "threshold", None),
        refusal=refusal, preconditions=tuple(full), tally=tally,
        restoration=restoration, slo_reads=reads, slo_supplied=supplied)


def refused(plan: ShadowPlan, link: str, reason: str, *, stage: str,
            where: Optional[tuple] = None,
            observations: Sequence[Observation] = ()) -> Promotion:
    """A ``REFUSE`` verdict for a precondition a CALLER owns.

    `revl.shadow_routing` schedules the shadow and therefore owns three checks
    this module cannot make: that the window is the declared action's, that it
    was served in the declared realm, and that the incumbent's answer was the
    one used. Those are preconditions in exactly the sense the walk in
    :func:`decide` uses, properties of the declaration read before any
    answer, so their refusals belong in this module's verdict shape rather
    than in a second one beside it.

    No tally is built and none can be: the caller reaches this before it calls
    :func:`decide`, so ``measurements_read`` is false and every stage of the
    gate is recorded as not reached. ``stage`` names the caller's own stage
    and is reported alongside them."""
    trail = [{"stage": stage, "status": "refused", "link": link}]
    return _verdict(plan, REFUSE, Refusal(link, reason, where), trail, None,
                    None, list(observations))


def decide(plan: ShadowPlan, observations: Sequence[Observation], *,
           key: Optional[bytes] = None,
           verifier: Optional[Callable] = None) -> Promotion:
    """The gate. ``PROMOTE``, ``REVERT`` or ``REFUSE``, with a named reason.

    The shape, and the shape IS the argument:

        plan -> route -> admit -> evidence -> policy -> authority -> state
        ---------------- preconditions --------------------------------
        || BARRIER
        accumulate -> divergence / sample / threshold
        --------------- measured --------------------------------------

    `route` comes before `admit` because it needs no evidence: a promotion to
    a role the action's own block does not name is refused without a record
    being read at all.

    Everything left of the barrier is a property of the declaration and of
    whether the evidence is evidence. Nothing left of it can see a tally,
    because `accumulate` has not been called, and nothing left of it can see a
    metric, because `admit` did not carry one across. A caller cannot weigh a
    latency win against a non-empty authority diff here, and not because the
    weighting is set to zero: there is no expression in this function in which
    both values appear.
    """
    observations = list(observations) if isinstance(
        observations, (list, tuple)) else []

    malformed = _check_plan(plan)
    if malformed is not None:
        return _verdict(plan, REFUSE, malformed, [], None, None, observations)

    trail = []
    for name, check in PLAN_PRECONDITIONS:
        refusal = check(plan, ())
        if refusal is not None:
            trail.append({"stage": name, "status": "refused",
                          "link": refusal.link})
            return _verdict(plan, REFUSE, refusal, trail, None, None,
                            observations)
        trail.append({"stage": name, "status": "satisfied"})

    pairs, refusal = admit(plan, observations, key=key, verifier=verifier)
    if refusal is not None:
        trail.append({"stage": "evidence", "status": "refused",
                      "link": refusal.link})
        return _verdict(plan, REFUSE, refusal, trail, None, None, observations)

    for name, check in PRECONDITIONS:
        refusal = check(plan, pairs)
        if refusal is not None:
            trail.append({"stage": name, "status": "refused",
                          "link": refusal.link})
            # The return is the barrier. No tally has been built, so the
            # verdict's `measurements_read` is false and the stages below are
            # recorded as not reached.
            return _verdict(plan, REFUSE, refusal, trail, None, None,
                            observations)
        trail.append({"stage": name, "status": "satisfied"})

    # ---------------------------------------------------------------- BARRIER
    tally = accumulate(plan.action_class, pairs)

    if plan.live and tally.first_divergence is not None:
        divergence = tally.first_divergence
        return _verdict(
            plan, REVERT,
            Refusal(DIVERGENCE_ATTRIBUTED,
                    f"the promoted role diverged at {divergence.describe()}; "
                    f"a live promotion reverts on the FIRST attributed "
                    f"divergence, whatever the accumulated ratio is, because "
                    f"the candidate's answer is the one in use",
                    divergence.crossing),
            trail, tally, _restoration(plan), observations)

    if tally.paired < plan.min_observations:
        return _verdict(
            plan, REFUSE,
            Refusal(SAMPLE_TOO_SMALL,
                    f"the window accumulated {tally.paired} pairs and the "
                    f"plan states {plan.min_observations}"),
            trail, tally, None, observations)

    if tally.agreement < plan.threshold:
        first = tally.first_divergence
        where = first.describe() if first else "no attributed divergence"
        return _verdict(
            plan, REFUSE,
            Refusal(AGREEMENT_BELOW_THRESHOLD,
                    f"accumulated agreement {tally.agreement:.4f} over "
                    f"{tally.paired} pairs is below the stated threshold "
                    f"{plan.threshold}; first divergence: {where}",
                    first.crossing if first else None),
            trail, tally, None, observations)

    return _verdict(plan, PROMOTE, None, trail, tally, None, observations)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def render(promotion: Promotion) -> str:
    """One report. A `REVERT` prints restored and compensated as two separate
    sentences; issue #1225's complaint is precisely that one word covered
    both."""
    plan_line = (f"{promotion.action_class[0]}.{promotion.action_class[1]}: "
                 f"{promotion.incumbent_role} -> {promotion.candidate_role}")
    out = [f"{promotion.kind} {promotion.version}", plan_line,
           f"decision: {promotion.decision}"]
    if promotion.refusal is not None:
        out.append(f"  {promotion.refusal.link}: {promotion.refusal.reason}")
    out.append("preconditions:")
    for entry in promotion.preconditions:
        link = f" ({entry['link']})" if entry.get("link") else ""
        out.append(f"  {entry['stage']}: {entry['status']}{link}")
    if promotion.tally is None:
        out.append(f"measured evidence: not read (metric blocks supplied: "
                   f"{promotion.slo_supplied}, read: {promotion.slo_reads})")
    else:
        tally = promotion.tally
        out.append(f"agreement: {tally.agreed}/{tally.paired} = "
                   f"{tally.agreement:.4f} (threshold {promotion.threshold})")
        for divergence in tally.divergences:
            out.append(f"  diverged at {divergence.describe()}")
        out.append(f"metric blocks supplied: {promotion.slo_supplied}, "
                   f"read: {promotion.slo_reads}")
    if promotion.restoration is not None:
        restoration = promotion.restoration
        restored = ", ".join(restoration["restored"]) or "nothing"
        out.append(f"restored: {restored}")
        if restoration["compensated"]:
            for entry in restoration["compensated"]:
                out.append(f"compensated only: {entry['layer']} "
                           f"({entry['state']}) via {entry['compensation']}")
        else:
            out.append("compensated only: nothing")
        if restoration["neither"]:
            for entry in restoration["neither"]:
                out.append(f"neither restored nor compensated: "
                           f"{entry['layer']} ({entry['state']})")
    return "\n".join(out)

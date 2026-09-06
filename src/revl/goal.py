"""The goal-contract termination lattice (roadmap item 441, controller slice 1).

Item 441's shape: the agent DRAFTS acceptance criteria, the system FREEZES them
at activation, and from then on a deterministic state machine reading OBSERVABLE
STATE — never the agent's own reply — owns every termination decision. This
module is that state machine's semantic kernel: the four contract states, their
declared order, and the per-turn fold. It is pure and reads no reply by
construction — there is no parameter here through which an agent's claim of
completion could reach the decision (docs/design/441-goal-contracts.md §3.2).

Everything else the controller needs — evaluating a criterion against real
state, the WAL trail, the two `Session.call` / `commit_confirm` chokepoint
terms — is a later slice built ON this kernel. The kernel ships first because it
carries the one property the whole feature turns on: the fold is a MAX over a
declared order, never an ``all(...)`` over booleans, so an empty or
partially-evaluated contract is never vacuously "done" (§3.2, the null-contract
attack). The order mirrors ``mcp.approval.worse`` (`approval.py:63`) rather than
inventing a second combining rule.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable


class GoalState(enum.Enum):
    """What the contract can honestly say about "done" this turn.

    Four states, and "done" is neither a fourth Verdict nor a property of one:
    a contract SELECTS which settling verdict is authorized, it is not itself a
    verdict (§3). The states, from the least element up:

      * ``SATISFIED``   — nothing left to say: every criterion achieved, every
        guard quiet, every instrument serving. Authorizes ``commit_confirm``.
      * ``UNDECIDED``   — a criterion cannot yet say yes. The run continues,
        gated exactly as today; no settlement is authorized.
      * ``UNEVALUABLE`` — a broken instrument (a verifier that has drifted to
        not-serving). Fail closed: no settlement, and forward class-(c)
        crossings raise to the operator.
      * ``VIOLATED``    — a guard fired: the run broke something. Only ``abort``
        settles.
    """

    SATISFIED = "satisfied"
    UNDECIDED = "undecided"
    UNEVALUABLE = "unevaluable"
    VIOLATED = "violated"


# The fail-closed lattice (§3.2): violated > unevaluable > undecided >
# satisfied. `satisfied` is the LEAST element, so a run is done only when no
# criterion, no guard and no instrument has anything to say. Read each ">":
# a fired guard is definite; a broken instrument is not evidence of success; a
# criterion that cannot yet tell is not a criterion that said yes.
_ORDER: dict[GoalState, int] = {
    GoalState.SATISFIED: 0,
    GoalState.UNDECIDED: 1,
    GoalState.UNEVALUABLE: 2,
    GoalState.VIOLATED: 3,
}


def worse(x: GoalState, y: GoalState) -> GoalState:
    """The worse (higher in the fail-closed order) of two contract states.

    The kernel's only combining rule, mirroring ``approval.worse``: one turn's
    state covers the whole contract or none of it.
    """
    return x if _ORDER[x] >= _ORDER[y] else y


def fold(states: Iterable[GoalState]) -> GoalState:
    """Fold a turn's per-finding states into the one contract state (§3.2).

    A MAX over the declared order, deliberately NOT ``all(...)`` over booleans:
    an ``all()`` over an empty or partially-evaluated list is vacuously true,
    and a vacuously satisfied contract is the null-contract attack in one line.
    An empty input is refused (``ValueError``) rather than folded to
    ``SATISFIED`` — a contract with zero criteria is refused at admission (§5.3,
    C3), so the fold never legitimately sees one, and treating empty as done
    would be exactly the attack this max exists to prevent.
    """
    result: GoalState | None = None
    for state in states:
        result = state if result is None else worse(result, state)
    if result is None:
        raise ValueError(
            "goal contract fold over zero findings: an empty contract is "
            "vacuous satisfaction and is refused at admission (item 441 §5.3)"
        )
    return result


def classify(role: str, value: bool | None, *, served: bool) -> GoalState:
    """Map one observed finding to its contract state — the point where the
    controller reads OBSERVABLE state and nothing else (§3.2, §7 E1/E2).

    ``role`` is ``"criterion"`` (a satisfaction predicate) or ``"guard"`` (a
    violation predicate). ``value`` is the finding's ``Opt[Bool]`` observation:
    ``None`` is ``Opt``-none (the verifier ran but cannot yet tell), ``True`` /
    ``False`` are ``Some(true)`` / ``Some(false)``. ``served`` is whether the
    verifier is actually serving this turn.

    Note there is no ``reply`` parameter: an agent's claim of completion has no
    path into this function. A drifted verifier is ``UNEVALUABLE``, never
    ``UNDECIDED`` (E2) — a broken instrument is not a proof the run may
    continue, which is item 427 F1's refuse-don't-degrade shape.
    """
    if role not in ("criterion", "guard"):
        raise ValueError(f"unknown finding role: {role!r}")
    if not served:
        return GoalState.UNEVALUABLE
    if value is None:
        # Served but cannot yet tell: undecided for both roles. A guard that
        # cannot report is not a proof of safety, and undecided already blocks
        # settlement.
        return GoalState.UNDECIDED
    if role == "criterion":
        # Some(true) achieved this criterion; Some(false) is "not yet".
        return GoalState.SATISFIED if value else GoalState.UNDECIDED
    # guard: Some(true) means the run broke something; Some(false) is quiet.
    return GoalState.VIOLATED if value else GoalState.SATISFIED

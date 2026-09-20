#!/usr/bin/env python3
"""The evolution controller: one lifecycle, one verdict, and an authority the
loop may not evolve.

WHY THIS EXISTS
---------------
Roadmap item 520 (issue #1194). The pieces of a self-evolving runtime landed
separately and each works: the repair loop (item 62), the operator cockpit
(63), derived semver (64), generation history and undo (65), and the
self-extending runtime's `propose`/swap/rollback with its EDGE-1 post-activation
health gate (item 334, `src/revl/gate.py`). What did not exist is the
CONTROLLER: no single lifecycle whose verdict binds a proposal, its evidence,
its admission decision, and its promotion or rollback, and no place that states
the boundary of what the system may change about itself.

This module is that lifecycle. It decides nothing about compilers, traffic or
models. It is a REDUCER over stage evidence, and its whole contribution is the
ORDER in which that evidence is allowed to matter.

THE INVARIANT
-------------
    The system may evolve its behaviour, but it may not unilaterally evolve
    the rules that govern its authority.

Stated as a sentence that is a policy, and the loop's proposal channel is
exactly the mechanism for proposing changes to policies. Issue #1223 (item 544)
argues the boundary has to be a capability the attenuation product refuses
rather than a rule a later generation can propose a change to. That is correct
and it is item 544's work, not this module's. What a controller can contribute,
and what is implemented here, is the other half of the same argument:

  * the kernel set is ENUMERATED in one place (`KERNEL_PATHS` below) instead of
    being understood, which is the precondition issue #1223 lists first;
  * the enumeration and the stage table are themselves FENCED
    (`AUTHORITY_FENCE`), so a proposal whose diff reaches the rules that judge
    it is refused BY NAME rather than judged by the rules it just edited. That
    is the word "unilaterally" turned into a check: a fenced proposal is routed
    to a human, not forbidden forever.

THE LIFECYCLE, AND WHICH STAGES ARE PRECONDITIONS
-------------------------------------------------
Seven stages and a terminal decision, in this order:

    trigger -> propose -> admit -> authority || shadow -> canary -> observe
    ---------------- preconditions ----------    ------- measured -------

The `||` is the barrier and it is the point of the whole module. Issue #1222
(item 543): a canary on one percent of traffic measures latency, refusal rate
and answer quality ON THE PATHS THAT TRAFFIC HAPPENS TO TAKE, while capability
reach and taint edges are STATIC properties of the program. The rare path that
widens reach is precisely the path a one percent sample does not exhibit. So
the authority diff cannot be one objective weighed beside the measured ones; it
has to gate ENTRY to shadow and canary.

`decide()` enforces that structurally rather than by weighting. It walks the
preconditions in order and RETURNS on the first one that is not satisfied,
before any measured record is read. The verdict it writes records
`measurements_read: false` and marks every measured stage `not reached`, so the
artifact itself shows that the SLO evidence was present and was not consulted.
There is no code path on which a measured stage's content can raise a
precondition's verdict, because the function has already returned.

FAILURE DIRECTION
-----------------
Fail-closed everywhere, with no third value. A stage is `verified` only when a
record says so AND the controller's own checks agree. Every other shape is a
failure with a NAMED code:

    no record for a stage                  -> STAGE_SKIPPED
    a record with no evidence              -> EVIDENCE_MISSING
    a record that reports failure          -> STAGE_FAILED
    a trigger with no named artifact       -> NO_TRIGGER
    a trigger that is a schedule           -> SCHEDULED_TRIGGER
    no retry budget declared               -> UNBOUNDED_RETRY
    the retry budget is spent              -> BUDGET_EXHAUSTED
    a granted decider service              -> FORBIDDEN_GRANT
    the diff could not be measured         -> DIFF_UNVERIFIED
    the declared diff understates the tree -> DIFF_DISAGREES
    the diff reaches the lifecycle's rules -> FENCE_TOUCHED
    the diff reaches the admission kernel  -> KERNEL_INTERSECTION
    a non-empty capability/taint/budget or
    retention diff                         -> AUTHORITY_WIDENED

`AUTHORITY_WIDENED` and `KERNEL_INTERSECTION` are separate codes on purpose.
Issue #1222's exit test asks for a refusal BY NAME on a non-empty diff, and a
single generic `PRECONDITION_FAILED` would satisfy the letter of it while
losing the thing a reader needs: which authority moved.

WHY THE VERDICT IS NOT A SCORE
------------------------------
Same argument item 536 (issue #1206) makes for the reward, one level up. Under
any weighting with positive weight on the measured stages, a candidate with a
marginally non-empty reachability diff and excellent canary numbers clears a
threshold. A lifecycle satisfiable in a way its own preconditions refuse is a
defect, not a design choice. So the decision is an enumeration of three
terminal shapes, never a number:

    PROMOTE    every precondition satisfied and every measurement satisfied
    ROLL_BACK  every precondition satisfied, a measurement failed. The
               candidate was entitled to be activated, so the undo is the
               verdict, not a refusal (this is item 334's `SWAP_REVERTED`
               shape, at the lifecycle level).
    REFUSE     a precondition is not satisfied. Nothing was activated, so
               there is nothing to roll back.

TERMINATION IS A PROOF OBLIGATION
---------------------------------
Item 520 states that a proposal which cannot reach a verdict must halt rather
than loop. A proposal therefore carries `attempt` and `budget`, and the
`propose` stage refuses an attempt past the budget (`BUDGET_EXHAUSTED`) and a
proposal that declares no budget at all (`UNBOUNDED_RETRY`). A missing budget
is the unbounded case, so it fails closed rather than defaulting to a number
this module invented.

WHAT SLICE 1 DOES NOT DO
------------------------
It does not run shadow traffic, a canary or a compile. Those stages' evidence
is supplied by tools the repository already owns or is landing beside this one
(`tools/evolution_reward.py`, `tools/heldout_scoring.py`, `src/revl/gate.py`'s
`Gate.propose`), and slice 2 wires the adapters. What is implemented here is
the reduction, the ordering barrier, the fence and the named refusals. A stage
with no adapter has no record, and a stage with no record FAILS.

USAGE
-----
    tools/evolution_controller.py --proposal proposal.json
    tools/evolution_controller.py --proposal proposal.json --json verdict.json

Exit status: 0 PROMOTE, 1 ROLL_BACK, 2 REFUSE.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Exit statuses. REFUSE is distinct from ROLL_BACK for the same reason
# `tools/heldout_scoring.py` separates REFUSED from DIVERGENT: a reader has to
# be able to tell "the candidate went live and was undone" from "the candidate
# never went live", and one non-zero status collapses exactly that.
PROMOTE = 0
ROLL_BACK = 1
REFUSE = 2

DECISIONS = ("PROMOTE", "ROLL_BACK", "REFUSE")
_EXIT = {"PROMOTE": PROMOTE, "ROLL_BACK": ROLL_BACK, "REFUSE": REFUSE}


# --------------------------------------------------------------- stage table

# The stages that nothing can buy past. Order is load-bearing: `decide()` walks
# this tuple and returns on the first unsatisfied member.
PRECONDITIONS = ("trigger", "propose", "admit", "authority")

# The stages whose evidence is a MEASUREMENT. They are read only after every
# precondition is satisfied, and never instead of one.
MEASURED = ("shadow", "canary", "observe")

STAGES = PRECONDITIONS + MEASURED

# What supplies each stage's evidence. Item 520 requires the lifecycle to cite,
# for each stage, the mechanism that supplies it; this table is that citation
# and it is rendered into every verdict.
SUPPLIED_BY = {
    "trigger": "a gap record naming the repository artifact it came from "
               "(item 535, tools/evolve_curriculum.py)",
    "propose": "the candidate record and its retry budget (item 536's "
               "Candidate{tree, base, scope})",
    "admit": "the standalone self-extension compile and the FORBIDDEN_GRANT "
             "rule (item 334, Gate.propose in src/revl/gate.py)",
    "authority": "the attenuation product plus this module's fence and kernel "
                 "enumeration (item 543 issue #1222, item 544 issue #1223)",
    "shadow": "a draw the candidate could not read (item 537, "
              "tools/heldout_scoring.py)",
    "canary": "the SLO contracts (docs/design/473-slo-contracts.md)",
    "observe": "the reward conjunction over repository artifacts (item 536, "
               "tools/evolution_reward.py)",
}


# --------------------------------------------------------------- the fence

# THE RULES THAT GOVERN THE LOOP'S AUTHORITY. A proposal whose diff reaches one
# of these is refused rather than judged, because a proposal that edited the
# judge has not been judged.
#
# What is NOT here matters as much as what is, and the reasoning is the one
# `tools/heldout_scoring.py` states for `HELD_OUT_FENCE`: `src/`, `selfhost/`,
# `backends/` and `crates/` are the SUBJECT of the loop. Fencing them would
# forbid the work the loop exists to produce. The fence is the judge, never the
# subject.
AUTHORITY_FENCE = (
    "tools/evolution_controller.py",
    "tests/test_evolution_controller.py",
)

# THE ADMISSION KERNEL, enumerated in one place because issue #1223 lists that
# as the first precondition of making the boundary a capability rather than a
# sentence. These are path PREFIXES, matched segment-wise.
#
# The last entry is the one issue #1223 says the wave currently files on the
# wrong side: retention. A loop permitted to "update indexes and retention
# policies" as ordinary behaviour tuning can extend its own `Retained[T, P]`
# deadline, which is an authority change wearing the clothes of a cache
# setting, and G-RETAIN is the guarantee it quietly relaxes.
KERNEL_PATHS = (
    "src/revl/admission.py",
    "src/revl/admit_profile.py",
    "src/revl/attest.py",
    "src/revl/taint.py",
    "src/revl/retention.py",
    "crates/revl-gate",
    "formal",
    "tools/gate_reference_census.py",
    "tools/gate_reference_census_baseline.json",
)

# Services whose grant is the non-extern path to the decider. The first two are
# `_DECIDER_SERVICES` in `src/revl/gate.py`, where `Gate.propose` refuses a
# granted set naming one with code `FORBIDDEN_GRANT`; the last two are the
# session and gate facades, added because a proposal record is written by hand
# or by a curriculum step and can name them where a composed IR cannot.
#
# Two things this list is NOT. It is not a superset check on the real rule:
# `gate.py` says in so many words that the name check "inspects the granted SET
# only" and that the structural enforcement is on the composed IR, against the
# `host_admit` crossing itself. And it is not a second implementation of that
# structural rule, which the controller has no IR to run. It is the name half,
# restated at the second entry point, so a proposal that never passed through
# `Gate.propose` still meets it. `tests/test_evolution_controller.py` holds it
# to `gate.py`'s set so the two cannot drift apart silently.
DECIDER_SERVICES = ("Admission", "AdmitGate", "Gate", "Session")

# The four authority axes the attenuation product accounts for
# (`docs/capability-attenuation.md`), plus retention per issue #1223.
AUTHORITY_AXES = ("capability", "taint", "budget", "realm", "retention")


# ------------------------------------------------------------------ verdicts

@dataclass(frozen=True)
class Verdict:
    """One stage's answer.

    The field names are item 536's (`tools/evolution_reward.py`), deliberately:
    a lifecycle whose stage answers have a different shape from the reward's
    component answers would need a translation layer, and a translation layer
    between two fail-closed checks is where a third value gets introduced.
    `verified` is the only value that is not a failure. There is no `unknown`
    and no `skipped`, because a third value is where a fail-open default hides.
    """

    component: str
    verified: bool
    reason: str
    evidence: tuple = ()
    code: str = ""

    def as_dict(self) -> dict:
        return {
            "component": self.component,
            "verdict": "verified" if self.verified else "failed",
            "reason": self.reason,
            "evidence": list(self.evidence),
            "code": self.code,
        }


def failed(component: str, code: str, reason: str, evidence=()) -> Verdict:
    return Verdict(component, False, reason, tuple(evidence), code)


def verified(component: str, reason: str, evidence=()) -> Verdict:
    return Verdict(component, True, reason, tuple(evidence), "")


def not_reached(component: str, blocker: str) -> Verdict:
    """A measured stage that was never read because a precondition refused.

    This is not a failure of the measurement and it is not a pass. It is the
    record that the barrier held, and it is written into the verdict so the
    artifact shows what was NOT consulted. `verified` is False because the only
    question `verified` answers is "may this contribute to a promotion", and
    the answer for an unread stage is no.
    """
    return Verdict(
        component, False,
        f"not reached: the `{blocker}` precondition is not satisfied, so this "
        f"measurement was never read", (), "NOT_REACHED")


# ----------------------------------------------------------------- candidate

# The only keys read off a proposal record. The whitelist is what makes "the
# controller does not read the candidate's account of itself" structural rather
# than a convention: a key that is not here never reaches a check, whatever it
# says. Item 536 uses the same mechanism and reports the dropped keys back.
RECORD_KEYS = ("tree", "base", "scope", "trigger", "granted", "attempt",
               "budget", "changed", "stages")

# The three that item 536's `Candidate` carries. Kept as a named subset so the
# two lanes can be held to the same shape by a test.
CANDIDATE_KEYS = ("tree", "base", "scope")


@dataclass(frozen=True)
class Candidate:
    """A proposal's scorable facts. Carries no prose field, by construction.

    Same three fields as item 536's `Candidate` (`tools/evolution_reward.py`),
    same names, same meaning: the tree the candidate produced, the base ref it
    is diffed against, and the declared scope.
    """

    tree: Path
    base: str
    scope: tuple = ()
    prose_ignored: tuple = ()


@dataclass(frozen=True)
class Proposal:
    """A candidate plus the lifecycle facts the controller needs to judge it."""

    candidate: Candidate
    trigger: dict = field(default_factory=dict)
    granted: tuple = ()
    attempt: object = None
    budget: object = None
    declared_changed: object = None
    stages: dict = field(default_factory=dict)

    @property
    def tree(self) -> Path:
        return self.candidate.tree

    @property
    def base(self) -> str:
        return self.candidate.base


def load_proposal(record: dict) -> Proposal:
    """Build a `Proposal`, dropping every key outside `RECORD_KEYS` by name.

    Refuses rather than guesses. A record with no `tree`, `base` or `scope`
    cannot be judged, and an unjudgeable proposal is not a promotable one. The
    three are exactly item 536's required keys, so a candidate record written
    for the reward scorer is a valid proposal record here.
    """
    missing = [k for k in CANDIDATE_KEYS if k not in record]
    if missing:
        raise ValueError(
            "proposal record is missing required key(s): " + ", ".join(missing))
    scope = record["scope"]
    if not isinstance(scope, (list, tuple)):
        raise ValueError("proposal `scope` must be a list of path globs")
    stages = record.get("stages") or {}
    if not isinstance(stages, dict):
        raise ValueError("proposal `stages` must be an object keyed by stage name")
    trigger = record.get("trigger") or {}
    if not isinstance(trigger, dict):
        raise ValueError("proposal `trigger` must be an object")
    granted = record.get("granted") or ()
    if not isinstance(granted, (list, tuple)):
        raise ValueError("proposal `granted` must be a list of service names")
    changed = record.get("changed")
    if changed is not None and not isinstance(changed, (list, tuple)):
        raise ValueError("proposal `changed` must be a list of paths")
    candidate = Candidate(
        tree=Path(record["tree"]),
        base=str(record["base"]),
        scope=tuple(str(s) for s in scope),
        prose_ignored=tuple(sorted(k for k in record if k not in RECORD_KEYS)),
    )
    return Proposal(
        candidate=candidate,
        trigger=dict(trigger),
        granted=tuple(str(g) for g in granted),
        attempt=record.get("attempt"),
        budget=record.get("budget"),
        declared_changed=(None if changed is None
                          else tuple(str(c) for c in changed)),
        stages=dict(stages),
    )


# --------------------------------------------------------------- the diff

def _git(tree: Path, args, timeout: int = 120):
    """`(ok, text)` for a read-only git command in the candidate tree."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(tree)] + [str(a) for a in args],
            timeout=timeout, capture_output=True, text=True,
            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"git {' '.join(str(a) for a in args)} failed: {exc}"
    if proc.returncode != 0:
        return False, proc.stderr.strip() or f"git exited {proc.returncode}"
    return True, proc.stdout


def measured_diff(proposal: Proposal):
    """`(ok, paths_or_error)`: the candidate's changed-file set, from git.

    Committed and uncommitted changes both count, because a proposal is judged
    on the tree it produced and not on how tidily it was committed. This is the
    same read `tools/evolution_reward.py` performs for its `scope` component.
    """
    tree = proposal.tree
    if not (tree / ".git").exists():
        return False, f"{tree} is not a git tree, so its diff cannot be measured"
    ok, out = _git(tree, ["diff", "--name-only", proposal.base])
    if not ok:
        return False, out
    paths = {line.strip() for line in out.splitlines() if line.strip()}
    ok, out = _git(tree, ["ls-files", "--others", "--exclude-standard"])
    if not ok:
        return False, out
    paths |= {line.strip() for line in out.splitlines() if line.strip()}
    return True, sorted(paths)


def _under(path: str, prefix: str) -> bool:
    """Segment-wise prefix match, so `formal` does not match `formalise.py`."""
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def fence_hits(changed, fence=AUTHORITY_FENCE):
    return sorted(p for p in changed if any(_under(p, f) for f in fence))


def kernel_hits(changed, kernel=KERNEL_PATHS):
    return sorted(p for p in changed if any(_under(p, k) for k in kernel))


def authority_moved(record: dict):
    """The axes on which a declared authority diff is non-empty.

    The record's `diff` is an object keyed by axis; an axis with a non-empty
    list is an authority that moved. An axis that is ABSENT is not empty, it is
    unmeasured, so it counts as moved: an unmeasured axis that promotes is the
    fail-open shape this whole module exists to refuse.
    """
    diff = record.get("diff")
    if not isinstance(diff, dict):
        return list(AUTHORITY_AXES), "the record carries no `diff` object"
    moved = []
    for axis in AUTHORITY_AXES:
        if axis not in diff:
            moved.append(axis)
            continue
        entries = diff[axis]
        if not isinstance(entries, (list, tuple)):
            moved.append(axis)
        elif entries:
            moved.append(axis)
    return moved, ""


# ------------------------------------------------------------- stage records

def _record_for(proposal: Proposal, stage: str):
    """`(ok, record_or_verdict)`: the stage's evidence record, or the refusal.

    Two named refusals and no third shape. A stage with no record at all was
    SKIPPED; a stage whose record carries no evidence is UNSUPPORTED. The
    second is the one the item warns about: a controller that promotes when a
    stage's evidence is MISSING (rather than failing) is the fail-open shape,
    and this repository has measured ten separate checks that ran on every PR
    and could not fail.
    """
    record = proposal.stages.get(stage)
    if record is None:
        return False, failed(
            stage, "STAGE_SKIPPED",
            f"no record for `{stage}`. The stage was skipped, and a skipped "
            f"stage is a failure, never a pass. Supplied by: "
            f"{SUPPLIED_BY[stage]}")
    if not isinstance(record, dict):
        return False, failed(
            stage, "STAGE_SKIPPED",
            f"the `{stage}` record is {type(record).__name__}, not an object")
    record = _normalise(stage, record)
    if not record.get("evidence"):
        return False, failed(
            stage, "EVIDENCE_MISSING",
            f"the `{stage}` record cites no evidence. A verdict with no "
            f"evidence is an assertion, and this lifecycle does not promote on "
            f"assertions. Supplied by: {SUPPLIED_BY[stage]}")
    return True, record


def _normalise(stage: str, record: dict) -> dict:
    """Accept a sibling tool's own output shape as a stage record.

    One adapter is implemented, and it is the one that pays for itself: item
    536's `Scorecard` (`{"retained", "blockers", "components", ...}`) is
    exactly the `observe` stage's evidence, so `tools/evolution_reward.py
    --json` is wired in without a translation step that could soften the
    conjunction. `retained` maps to `verified` because it is the same
    all-or-nothing question, and the component names become the evidence.
    """
    if "retained" not in record:
        return record
    components = record.get("components") or []
    names = [c.get("component") for c in components if isinstance(c, dict)]
    blockers = list(record.get("blockers") or [])
    return {
        "verified": bool(record.get("retained")),
        "reason": ("every reward component verified" if record.get("retained")
                   else "reward components did not verify: " + ", ".join(blockers)),
        "evidence": [f"tools/evolution_reward.py: {n}" for n in names if n],
        "diff": record.get("diff"),
    }


def _plain(stage: str, record: dict) -> Verdict:
    """The generic read of a stage record: `verified`, with its evidence."""
    evidence = tuple(str(e) for e in record.get("evidence") or ())
    reason = str(record.get("reason") or "")
    if not record.get("verified"):
        return failed(
            stage, "STAGE_FAILED",
            reason or f"the `{stage}` record reports failure", evidence)
    return verified(stage, reason or f"`{stage}` verified", evidence)


# ---------------------------------------------------------------- the stages

def judge_trigger(proposal: Proposal) -> Verdict:
    """The lifecycle is EVENT-DRIVEN: a gap triggers it, not a schedule.

    Item 520 names this as one of the two inputs the run loop must not own. It
    is checkable, so it is checked rather than documented: a trigger must name
    the repository artifact the gap was read off (item 535's curriculum tasks
    each carry that name), and a trigger whose `kind` is a schedule is refused.
    A loop that evolves because the clock said so has no gap to close, and the
    reward it then maximises is the empty diff (issue #1224).
    """
    ok, record = _record_for(proposal, "trigger")
    if not ok:
        return record
    kind = str(record.get("kind") or proposal.trigger.get("kind") or "")
    artifact = str(record.get("artifact") or proposal.trigger.get("artifact") or "")
    gap = str(record.get("gap") or proposal.trigger.get("gap") or "")
    if kind == "schedule":
        return failed(
            "trigger", "SCHEDULED_TRIGGER",
            "the trigger is a schedule. Item 520 requires the lifecycle to be "
            "event-driven: a gap starts it, not the clock. A scheduled loop has "
            "no gap to close and maximises the empty diff",
            tuple(record.get("evidence") or ()))
    if not artifact or not gap:
        return failed(
            "trigger", "NO_TRIGGER",
            "the trigger names no gap and/or no repository artifact it was read "
            "off. A trigger that cannot name its artifact is a schedule that "
            "has not admitted it is one",
            tuple(record.get("evidence") or ()))
    base = _plain("trigger", record)
    if not base.verified:
        return base
    return verified(
        "trigger", f"gap `{gap}` read off `{artifact}`",
        base.evidence + (f"artifact={artifact}",))


def judge_propose(proposal: Proposal) -> Verdict:
    """The candidate record, its scope, and the TERMINATION obligation.

    Item 520: a proposal that cannot reach a verdict halts rather than loops.
    That is a proof obligation about the loop, and the checkable projection of
    it onto one proposal is the retry budget. A proposal with no declared
    budget is the unbounded case and fails closed; a proposal past its budget
    HALTS with a named code instead of being handed back to the proposer.
    """
    ok, record = _record_for(proposal, "propose")
    if not ok:
        return record
    if not proposal.candidate.scope:
        return failed(
            "propose", "UNSCOPED",
            "the proposal declared no scope, so nothing bounds what it may "
            "change. An unbounded scope cannot be satisfied and is not a pass")
    budget = proposal.budget
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        return failed(
            "propose", "UNBOUNDED_RETRY",
            "the proposal declares no positive retry `budget`. Termination is a "
            "proof obligation here (item 520), and an unbounded retry count is "
            "the case that cannot discharge it. The controller does not invent "
            "a default, because a default is a bound nobody chose")
    attempt = proposal.attempt
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        return failed(
            "propose", "UNBOUNDED_RETRY",
            "the proposal declares no positive `attempt` number, so its "
            "position against the budget is unknown")
    if attempt > budget:
        return failed(
            "propose", "BUDGET_EXHAUSTED",
            f"attempt {attempt} is past the declared budget of {budget}. The "
            f"proposal halts rather than looping; the way forward is a human "
            f"reading the attempt history, not another generation")
    base = _plain("propose", record)
    if not base.verified:
        return base
    return verified(
        "propose", f"attempt {attempt} of {budget}, scope declared",
        base.evidence + (f"scope={';'.join(proposal.candidate.scope)}",))


def judge_admit(proposal: Proposal) -> Verdict:
    """The admission decision, plus the forbidden-grant rule restated.

    The compile itself is item 334's (`Gate.propose` compiles the candidate
    STANDALONE under `AdmissionProfile.self_extension(granted)`); its verdict
    arrives here as the stage record. What the controller adds is the
    FORBIDDEN_GRANT rule a second time, over the proposal's own `granted` set.

    Restating it is not redundancy, and it is not a claim to have reproduced
    it. `Gate.propose` enforces the rule STRUCTURALLY on the composed IR,
    against the `host_admit` crossing; this is the NAME half only, and it
    catches a decider composed under its stdlib service name. It is restated
    because the controller judges proposals that never passed through
    `Gate.propose` (a curriculum task, a replayed record, a candidate admitted
    by a future adapter), and a rule enforced at only one of two entry points
    is a rule with a way around it. A proposal that DID pass through
    `Gate.propose` has already met the stronger form.
    """
    forbidden = sorted(set(proposal.granted) & set(DECIDER_SERVICES))
    if forbidden:
        return failed(
            "admit", "FORBIDDEN_GRANT",
            f"the granted set names a gate/session/admit-control service "
            f"({', '.join(forbidden)}). Granting a decider service is the "
            f"non-extern path to the decider that the untrusted-author profile "
            f"does not close (item 334, `_DECIDER_SERVICES` in "
            f"src/revl/gate.py). Re-entrant propose is deferred and this rule "
            f"enforces it",
            (f"granted={';'.join(proposal.granted)}",))
    ok, record = _record_for(proposal, "admit")
    if not ok:
        return record
    return _plain("admit", record)


def judge_authority(proposal: Proposal) -> Verdict:
    """The authority diff. A PRECONDITION, and the reason this module exists.

    Four checks, every one of them computed by the controller rather than read
    off the record, because a proposal that can hand in a green authority
    record and be believed has not had its authority checked.

      1. THE DIFF IS MEASURED, not declared. When the tree is a git tree the
         changed-file set comes from git and wins. A declared set that
         understates it is `DIFF_DISAGREES`; a tree that cannot be read at all
         is `DIFF_UNVERIFIED` and cannot promote. A self-reported diff is the
         candidate's account of itself, which is the one input this lifecycle
         does not accept.
      2. THE FENCE. A diff reaching this module or its tests is refused: the
         proposal would be judged by rules it just edited. This is the
         invariant turned into a check.
      3. THE KERNEL. A diff reaching `KERNEL_PATHS` is refused by name, citing
         the guarantee it defends, per issue #1223's exit test.
      4. THE AXES. A non-empty capability, taint, budget, realm or retention
         diff is refused by name, per issue #1222's exit test. An axis that is
         absent from the record counts as moved, not as empty.
    """
    ok, record = _record_for(proposal, "authority")
    if not ok:
        return record

    ok, measured = measured_diff(proposal)
    declared = proposal.declared_changed
    if not ok:
        return failed(
            "authority", "DIFF_UNVERIFIED",
            f"the changed-file set could not be measured ({measured}). A "
            f"declared diff is the candidate's account of itself, so it cannot "
            f"stand in for one: this stage has no satisfiable path on a tree "
            f"nobody can read",
            (f"tree={proposal.tree}",))
    if declared is not None:
        understated = sorted(set(measured) - set(declared))
        if understated:
            return failed(
                "authority", "DIFF_DISAGREES",
                "the declared changed-file set understates the tree by "
                + str(len(understated)) + " path(s): "
                + ", ".join(understated[:6]),
                (f"git diff --name-only {proposal.base}",))

    hits = fence_hits(measured)
    if hits:
        return failed(
            "authority", "FENCE_TOUCHED",
            "the diff reaches the rules that govern this lifecycle's authority "
            "(" + ", ".join(hits) + "). The system may evolve its behaviour, "
            "but it may not UNILATERALLY evolve the rules that govern its "
            "authority: this proposal would be judged by rules it edited. It "
            "is routed to a human, not forbidden",
            (f"fence={';'.join(AUTHORITY_FENCE)}",))

    hits = kernel_hits(measured)
    if hits:
        return failed(
            "authority", "KERNEL_INTERSECTION",
            "the diff reaches the admission kernel (" + ", ".join(hits)
            + "). The kernel decides what may be admitted at all, so a "
              "generation that edits it is deciding its own authority. The "
              "guarantees defended here are G8 (capability containment) and "
              "G-RETAIN (a retention deadline is an authority, not a cache "
              "setting)",
            (f"kernel={';'.join(KERNEL_PATHS)}",))

    moved, why = authority_moved(record)
    if moved:
        return failed(
            "authority", "AUTHORITY_WIDENED",
            "the authority diff is not empty on: " + ", ".join(moved)
            + (f" ({why})" if why else "")
            + ". Capability reach and taint edges are STATIC properties of the "
              "program, and a one percent canary measures the paths traffic "
              "happens to take, so no measured objective may buy this (item "
              "543, issue #1222). Refused before shadow and canary are read",
            tuple(record.get("evidence") or ()))

    base = _plain("authority", record)
    if not base.verified:
        return base
    return verified(
        "authority",
        f"authority diff empty on all {len(AUTHORITY_AXES)} axes; "
        f"{len(measured)} measured path(s), none in the fence or the kernel",
        base.evidence + (f"git diff --name-only {proposal.base}",
                         f"axes={';'.join(AUTHORITY_AXES)}"))


def judge_measured(proposal: Proposal, stage: str) -> Verdict:
    ok, record = _record_for(proposal, stage)
    if not ok:
        return record
    return _plain(stage, record)


JUDGES = {
    "trigger": judge_trigger,
    "propose": judge_propose,
    "admit": judge_admit,
    "authority": judge_authority,
}


# ----------------------------------------------------------------- the verdict

@dataclass
class LifecycleVerdict:
    """The single recorded verdict item 520 asks for.

    It binds, in one object: the proposal, one verdict per stage in lifecycle
    order, whether the measured stages were read at all, the terminal decision,
    and the named code that produced it. Nothing else in this module records a
    decision, so there is exactly one place a reader has to look and exactly
    one place a future stage has to be added.
    """

    proposal: Proposal
    verdicts: list = field(default_factory=list)
    decision: str = "REFUSE"
    code: str = ""
    measurements_read: bool = False
    findings: list = field(default_factory=list)

    def by_name(self) -> dict:
        return {v.component: v for v in self.verdicts}

    @property
    def refusing_stage(self) -> str:
        for v in self.verdicts:
            if not v.verified and v.code != "NOT_REACHED":
                return v.component
        return ""

    def as_dict(self) -> dict:
        return {
            "decision": self.decision,
            "code": self.code,
            "measurements_read": self.measurements_read,
            "refusing_stage": self.refusing_stage,
            "findings": list(self.findings),
            "stages": [v.as_dict() for v in self.verdicts],
            "preconditions": list(PRECONDITIONS),
            "measured": list(MEASURED),
            "supplied_by": dict(SUPPLIED_BY),
            "base": self.proposal.base,
            "scope": list(self.proposal.candidate.scope),
            "attempt": self.proposal.attempt,
            "budget": self.proposal.budget,
            "prose_ignored": list(self.proposal.candidate.prose_ignored),
        }

    def render(self) -> str:
        lines = [f"evolution controller over {self.proposal.tree}",
                 f"base {self.proposal.base}", ""]
        for v in self.verdicts:
            kind = "precondition" if v.component in PRECONDITIONS else "measured"
            mark = "ok  " if v.verified else "FAIL"
            lines.append(f"  {mark} {v.component:<10} [{kind:<12}] {v.reason}")
        lines.append("")
        if not self.measurements_read:
            lines.append(
                "the measured stages were NOT read: a precondition refused "
                "first, and no measured objective may buy a precondition.")
        for note in self.findings:
            lines.append(f"finding: {note}")
        lines.append("")
        if self.decision == "PROMOTE":
            lines.append("PROMOTE: every precondition satisfied and every "
                         "measurement satisfied.")
        elif self.decision == "ROLL_BACK":
            lines.append(
                f"ROLL BACK ({self.code}): the preconditions were satisfied so "
                f"the candidate was entitled to activate, and a measurement "
                f"then failed. The undo is the verdict.")
        else:
            lines.append(
                f"REFUSE ({self.code}) at `{self.refusing_stage}`: nothing was "
                f"activated, so there is nothing to roll back.")
        if self.proposal.candidate.prose_ignored:
            lines.append(
                "proposal keys ignored (never read by any stage): "
                + ", ".join(self.proposal.candidate.prose_ignored))
        return "\n".join(lines)


def _judge(proposal: Proposal, stage: str) -> Verdict:
    """One stage's verdict. A judge that RAISES is a failed stage, never a
    crashed controller: an exception inside a judge is exactly the case where
    a `try`-less implementation would abort and leave a human to decide what it
    meant, which is the fail-open default by another route."""
    judge = JUDGES.get(stage)
    try:
        verdict = judge(proposal) if judge else judge_measured(proposal, stage)
    except Exception as exc:  # fail-closed
        return failed(stage, "JUDGE_RAISED",
                      f"the `{stage}` judge raised {type(exc).__name__}: {exc}")
    if verdict.component != stage:
        return failed(stage, "JUDGE_RAISED",
                      f"the judge answered for {verdict.component!r}, not {stage!r}")
    return verdict


def decide(proposal: Proposal) -> LifecycleVerdict:
    """The reduction. The ORDER of this function is the design.

    The preconditions are walked first, and the function RETURNS on the first
    one that is not satisfied, before a single measured record has been read.
    That is item 543's barrier expressed as control flow rather than as a
    weighting: there is no path on which a canary number can reach a comparison
    with a precondition's verdict, because the comparison does not exist.
    """
    verdicts = []
    for stage in PRECONDITIONS:
        verdict = _judge(proposal, stage)
        verdicts.append(verdict)
        if not verdict.verified:
            # The barrier. Every measured stage is recorded as unread, and a
            # measured record that exists ANYWAY is a finding: evidence that
            # could not legitimately have been produced means a shadow or
            # canary ran without the authority to run.
            findings = []
            supplied = sorted(s for s in MEASURED if s in proposal.stages)
            if supplied:
                findings.append(
                    "OUT_OF_ORDER: " + ", ".join(supplied) + " evidence was "
                    "supplied although the `" + stage + "` precondition is not "
                    "satisfied. A measurement that should not exist is not "
                    "reassurance, it is a report that a stage ran without the "
                    "authority to run.")
            for rest in PRECONDITIONS[PRECONDITIONS.index(stage) + 1:]:
                verdicts.append(not_reached(rest, stage))
            for measured in MEASURED:
                verdicts.append(not_reached(measured, stage))
            return LifecycleVerdict(
                proposal, verdicts, "REFUSE", verdict.code,
                measurements_read=False, findings=findings)

    # Only now. Every precondition is satisfied, so the candidate was entitled
    # to be activated and the measurements decide keep versus roll back.
    failures = []
    for stage in MEASURED:
        verdict = _judge(proposal, stage)
        verdicts.append(verdict)
        if not verdict.verified:
            failures.append(verdict)
    if failures:
        return LifecycleVerdict(
            proposal, verdicts, "ROLL_BACK", failures[0].code,
            measurements_read=True,
            findings=["measured stage(s) failed: "
                      + ", ".join(v.component for v in failures)])
    return LifecycleVerdict(proposal, verdicts, "PROMOTE", "",
                            measurements_read=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--proposal", type=Path, required=True,
                    help="JSON record: tree/base/scope plus trigger, granted, "
                         "attempt, budget and the per-stage evidence")
    ap.add_argument("--json", type=Path, help="write the verdict here")
    args = ap.parse_args(argv)

    try:
        record = json.loads(args.proposal.read_text())
    except (OSError, ValueError) as exc:
        print(f"evolution_controller: cannot read {args.proposal}: {exc}",
              file=sys.stderr)
        return REFUSE
    try:
        proposal = load_proposal(record)
    except ValueError as exc:
        print(f"evolution_controller: {exc}", file=sys.stderr)
        return REFUSE

    verdict = decide(proposal)
    print(verdict.render())
    if args.json:
        args.json.write_text(
            json.dumps(verdict.as_dict(), indent=1, sort_keys=True) + "\n")
    return _EXIT[verdict.decision]


if __name__ == "__main__":
    raise SystemExit(main())

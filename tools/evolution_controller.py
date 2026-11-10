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

  * the whole evaluator is ENUMERATED in one place, `EVALUATOR_PATHS` in
    `src/revl/kernel_boundary.py`, whose `KERNEL_PATHS` is one view and whose
    `authority` view is `AUTHORITY_FENCE` below. This module IMPORTS the views
    rather than restating them, which is the precondition issue #1223 lists
    first and issue #2802 extended to the scorecard assembler;
  * the enumeration and the stage table are themselves FENCED
    (`AUTHORITY_FENCE`), so a proposal whose diff reaches the rules that judge
    it is refused BY NAME rather than judged by the rules it just edited. That
    is the word "unilaterally" turned into a check: a fenced proposal is routed
    to a human, not forbidden forever;
  * the editable set is the OPERATOR's (`--allow` or `ALLOW_ENV`), not the
    proposal's. A measured diff outside it is refused at the `authority`
    precondition, so the measured stages are never read for it. The declared
    scope stays the candidate's claim; the allow set is the grant that claim is
    checked against, and no grant is default deny (issue #2803);
  * the verdict and every scorecard carry `protocol_digest`, the evaluator's
    identity from the scorer's own checkout (issue #2802). A stage record from
    a different checkout is refused rather than read, so the two rounds cannot
    silently claim they were scored by the same protocol.

THE LIFECYCLE, AND WHICH STAGES ARE PRECONDITIONS
-------------------------------------------------
Seven stages and a terminal decision, in this order:

    trigger -> propose -> admit -> authority || shadow -> canary -> observe
    ---------------- preconditions ----------    ------- measured -------

THE THREE MODES, AND ONE OF THEM MAY NOT ACT
--------------------------------------------
Item 520 splits the work this lifecycle governs three ways, and the split is the
proposal's `mode`: `repair` fixes a fault a trace has already localized,
`feature` implements a declared gap against the tests the gap names, and
`evolution` proposes a behaviour change. The mode is REQUIRED and has no
default. It is declared by the caller, so the controller cannot verify intent;
what it enforces is the SHAPE of the path each mode is allowed to take.

A `repair` proposal cites the localized fault (`fault`: a failing test or trace
id). A `feature` proposal cites the tests its gap names (`tests`). A proposal
missing its citation is refused at `propose`, before any measured record is
read.

An `evolution` proposal ENDS at `proposal-recorded`. It does not reach `admit`,
`authority`, `shadow`, `canary` or `observe`: the controller returns as soon as
`propose` verifies and records every later stage `NOT_REACHED`. Once `trigger`
verifies, a record that supplies evidence for one of those stages is refused
with `MODE_REFUSES_ACTIVATION`; a missing or failing trigger refuses first, at
`trigger`, because the preconditions stay in order. Either way the evolution
path has no door to activation rather than a label the controller trusts.

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
    a record from another evaluator        -> PROTOCOL_MISMATCH
    a record that reports failure          -> STAGE_FAILED
    a trigger with no named artifact       -> NO_TRIGGER
    a trigger that is a schedule           -> SCHEDULED_TRIGGER
    no retry budget declared               -> UNBOUNDED_RETRY
    the retry budget is spent              -> BUDGET_EXHAUSTED
    a proposal with no scope               -> UNSCOPED
    no declared mode                       -> MODE_MISSING
    a mode outside the three               -> MODE_UNKNOWN
    a repair with no fault citation        -> FAULT_NOT_CITED
    a feature with no tests citation       -> TESTS_NOT_CITED
    an evolution proposal asking for a
    stage past the proposal                -> MODE_REFUSES_ACTIVATION
    a granted decider service              -> FORBIDDEN_GRANT
    the diff could not be measured         -> DIFF_UNVERIFIED
    the declared diff understates the tree -> DIFF_DISAGREES
    no operator allow set declared         -> NO_ALLOW_SET
    the diff leaves the operator allow set -> OUTSIDE_ALLOW
    the diff reaches the lifecycle's rules -> FENCE_TOUCHED
    the diff reaches the admission kernel  -> KERNEL_INTERSECTION
    a non-empty capability/taint/budget or
    retention diff                         -> AUTHORITY_WIDENED
    a judge raised, or answered for the
    wrong stage                            -> JUDGE_RAISED
    the controller cannot name its own
    evaluator                              -> PROTOCOL_UNAVAILABLE

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
defect, not a design choice. So the decision is an enumeration of four
terminal shapes, never a number:

    PROMOTE            every precondition satisfied and every measurement
                       satisfied
    ROLL_BACK          a measurement failed AND the candidate had gone live (a
                       `canary` record exists). The undo is the verdict. This
                       is item 334's `SWAP_REVERTED` shape, at the lifecycle
                       level.
    REFUSE             a precondition is not satisfied, or a measurement failed
                       while the candidate was never activated. There is
                       nothing to undo, and reporting a rollback would claim an
                       undo that never ran.
    proposal-recorded  the proposal is in `evolution` mode, its citation is in
                       order, and it asks for nothing past the proposal. This
                       is not a promotion and not a refusal: for that mode the
                       lifecycle's product is the recorded proposal itself.

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
    tools/evolution_controller.py --proposal proposal.json --allow 'src/**,tests/**'
    tools/evolution_controller.py --proposal proposal.json --json verdict.json

`--allow` (repeatable, or comma-separated) is the operator's editable set; it
falls back to `ALLOW_ENV`. With no allow set the `authority` precondition
refuses, because the editable set belongs to whoever starts the run.

Exit status: 0 PROMOTE, 1 ROLL_BACK, 2 REFUSE, 3 proposal-recorded.
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
sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from revl.kernel_boundary import (  # noqa: E402
    KERNEL_PATHS, evaluator_fence, protocol_digest)
from revl.promotion_barrier import AUTHORITY_AXES  # noqa: E402

# Item 536's verdict, IMPORTED (issue #1336). The stage verdict below EXTENDS
# it; it does not restate it. See `Verdict`.
from evolution_reward import Verdict as ComponentVerdict  # noqa: E402
from evolution_reward import (  # noqa: E402
    ALLOW_ENV, _in_scope, allow_from_env, parse_allow)

# The modes of work item 520 splits the lifecycle's subject into. The mode is
# declared by the caller, so it is not an intent the controller can verify:
# what it decides is which path the record is allowed to take. See the docstring
# section "THE THREE MODES".
MODES = ("repair", "feature", "evolution")

# The terminal shape for an evolution-mode proposal. Spelled as the roadmap
# writes it, because it is not one of the three decisions proper (`PROMOTE`,
# `ROLL_BACK`, `REFUSE`): the lifecycle did not promote, roll back or refuse
# anything; it recorded a proposal and stopped.
PROPOSAL_RECORDED = "proposal-recorded"

# Exit statuses. REFUSE is distinct from ROLL_BACK for the same reason
# `tools/heldout_scoring.py` separates REFUSED from DIVERGENT: a reader has to
# be able to tell "the candidate went live and was undone" from "the candidate
# never went live", and one non-zero status collapses exactly that. RECORDED is
# distinct again because a recorded proposal is neither: it is the successful
# product of evolution mode, and a caller that cannot tell it from a promotion
# would treat a proposal as an activation.
PROMOTE = 0
ROLL_BACK = 1
REFUSE = 2
RECORDED = 3

DECISIONS = ("PROMOTE", "ROLL_BACK", "REFUSE", PROPOSAL_RECORDED)
_EXIT = {"PROMOTE": PROMOTE, "ROLL_BACK": ROLL_BACK, "REFUSE": REFUSE,
         PROPOSAL_RECORDED: RECORDED}


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
    "authority": "the attenuation product, the operator allow set "
                 "(--allow / REVL_EDIT_ALLOW), plus this module's fence and "
                 "kernel enumeration (item 543 issue #1222, item 544 issue "
                 "#1223, issue #2803)",
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
# This is the `authority` VIEW of `EVALUATOR_PATHS` (issue #2802), not a list of
# its own: the one enumeration lives next to `KERNEL_PATHS` in
# `src/revl/kernel_boundary.py`, and it carries the controller, the reward, the
# progress counters, the held-out scorer and their tests. A path that used to be
# on no list (the scorecard assembler) is on this one because a proposal that
# edits it is judged by the rule it just wrote.
#
# What is NOT here matters as much as what is, and the reasoning is the one
# `tools/heldout_scoring.py` states for `HELD_OUT_FENCE`: `src/`, `selfhost/`,
# `backends/` and `crates/` are the SUBJECT of the loop. Fencing them would
# forbid the work the loop exists to produce. The fence is the judge, never the
# subject.
AUTHORITY_FENCE = evaluator_fence("authority")

#: This checkout's evaluator identity (issue #2802), for the verdict in
#: progress. Computed in `decide()` rather than at import for two reasons: a
#: checkout whose protocol cannot be read is a named refusal rather than an
#: import traceback, and a controller asked twice compares against the protocol
#: it will actually run, not the one it saw at start-up.
_OWN_PROTOCOL = None


def own_protocol_digest() -> str:
    """`protocol_digest(ROOT)`, computed once per `decide()`."""
    global _OWN_PROTOCOL
    if _OWN_PROTOCOL is None:
        _OWN_PROTOCOL = protocol_digest(ROOT)
    return _OWN_PROTOCOL

# THE ADMISSION KERNEL, enumerated in one place because issue #1223 lists that
# as the first precondition of making the boundary a capability rather than a
# sentence. These are path PREFIXES, matched segment-wise.
#
# The last entry is the one issue #1223 says the wave currently files on the
# wrong side: retention. A loop permitted to "update indexes and retention
# policies" as ordinary behaviour tuning can extend its own `Retained[T, P]`
# deadline, which is an authority change wearing the clothes of a cache
# setting, and G-RETAIN is the guarantee it quietly relaxes.
# KERNEL_PATHS is IMPORTED, not restated. It used to be copied here, and the
# copy had already drifted: `src/revl/kernel_boundary.py` itself was in the
# definition and missing from this one, so the diff-side check did not defend
# the file that says what the kernel is. A loop permitted to edit that file
# could widen the boundary and then pass the widened check.

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

# The authority axes the attenuation product accounts for
# (`docs/capability-attenuation.md`), plus retention per issue #1223.
# IMPORTED from `src/revl/promotion_barrier.py`, not restated, for the
# reason KERNEL_PATHS is: that module says in so many words that it is the
# canonical axis set and that `AXIS_ALIASES` maps every other spelling in
# the tree onto it, and this file had already shipped one copied constant
# that drifted. An axis added there is measured here on the next run
# instead of being read as UNMEASURED by a stale tuple.


# ------------------------------------------------------------------ verdicts

@dataclass(frozen=True)
class Verdict(ComponentVerdict):
    """One stage's answer: item 536's component verdict, plus a named code.

    The four fields `component`, `verified`, `reason` and `evidence`, and the
    four keys `as_dict` spells for them, are item 536's
    (`tools/evolution_reward.py::Verdict`). They are INHERITED, not restated. A
    lifecycle whose stage answers had a different shape from the reward's
    component answers would need a translation layer, and a translation layer
    between two fail-closed checks is where a third value gets introduced.
    `verified` is the only value that is not a failure. There is no `unknown`
    and no `skipped`, because a third value is where a fail-open default hides.

    `code` IS THIS MODULE'S FIFTH FIELD and it is deliberate, not drift. Issue
    #1222's exit test asks for a refusal BY NAME, and the FAILURE DIRECTION
    table in this module's docstring enumerates the codes a stage can
    fail with. Item 536's reward has no such enumeration: a reward component
    fails with prose, because the reward answers "did this candidate earn a
    training example" and the lifecycle answers "which authority refused, and
    at which stage". A code is the lifecycle's question, so it lives on the
    lifecycle's verdict.

    Until issue #1336 this was a third HAND-KEPT COPY of the four fields whose
    docstring said they were item 536's. They were not: this copy had already
    grown `code`, so the docstring asserted an agreement that did not hold, in
    the place a reader is most likely to trust. Inheriting makes the agreement
    structural in the direction that matters -- a field or key item 536 adds or
    renames arrives here on the next import instead of being read as absent by
    a stale list -- and leaves `code` as the ONE difference, declared once.
    """

    code: str = ""

    def as_dict(self) -> dict:
        """Item 536's four keys, verbatim from its `as_dict`, plus `code`."""
        return {**super().as_dict(), "code": self.code}


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


def recorded_only(component: str, mode: str) -> Verdict:
    """A stage an evolution-mode proposal may not reach, recorded not read.

    This is `not_reached`'s sibling for the mode stop. The precondition did not
    fail: the lifecycle ended at the proposal because that is the whole of what
    evolution mode may produce. Saying "the propose precondition is not
    satisfied" here would be false, and a false reason in a verdict is worse
    than no reason.
    """
    return Verdict(
        component, False,
        f"not reached: the proposal is in `{mode}` mode, which produces a "
        f"proposal only and may not admit, activate or deploy", (),
        "NOT_REACHED")


# ----------------------------------------------------------------- candidate

# The only keys read off a proposal record. The whitelist is what makes "the
# controller does not read the candidate's account of itself" structural rather
# than a convention: a key that is not here never reaches a check, whatever it
# says. Item 536 uses the same mechanism and reports the dropped keys back.
#
# `mode` and the two citation keys are this module's, not item 536's. They are
# here so that a record written for the reward scorer stays a valid proposal
# record only when it also declares which mode of work it is, which is item
# 520's rule and not a field the reward has an opinion about.
RECORD_KEYS = ("tree", "base", "scope", "mode", "fault", "tests", "trigger",
               "granted", "attempt", "budget", "changed", "stages")

# The three that item 536's `Candidate` carries. Kept as a named subset so the
# two lanes can be held to the same shape by a test.
CANDIDATE_KEYS = ("tree", "base", "scope")


@dataclass(frozen=True)
class Candidate:
    """A proposal's scorable facts. Carries no prose field, by construction.

    Same three fields as item 536's `Candidate` (`tools/evolution_reward.py`),
    same names, same meaning: the tree the candidate produced, the base ref it
    is diffed against, and the declared scope. `allow` is the OPERATOR's grant
    and is not a record key.
    """

    tree: Path
    base: str
    scope: tuple = ()
    allow: tuple = ()
    prose_ignored: tuple = ()


@dataclass(frozen=True)
class Proposal:
    """A candidate plus the lifecycle facts the controller needs to judge it.

    `mode` has no default. It is read raw off the record so that an absent or
    unknown value is refused by the `propose` stage with a named code, rather
    than replaced with one of the three modes here. A default is exactly the
    convention this field exists to remove.
    """

    candidate: Candidate
    mode: object
    fault: object = None
    tests: object = None
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


def load_proposal(record: dict, allow=()) -> Proposal:
    """Build a `Proposal`, dropping every key outside `RECORD_KEYS` by name.

    Refuses rather than guesses. A record with no `tree`, `base` or `scope`
    cannot be judged, and an unjudgeable proposal is not a promotable one. The
    three are exactly item 536's required keys, so a candidate record written
    for the reward scorer is a valid proposal record here once it declares the
    `mode` item 520 requires.

    `mode`, `fault` and `tests` are read raw and validated by `judge_propose`:
    a missing mode, a mode outside `MODES`, and a mode's citation are failures
    the verdict has to name, not parse errors that leave only stderr. The
    record's shape is checked here; what the lifecycle permits is checked
    there.

    `allow` is the operator's editable set, passed in by the caller. A record
    that carries an `allow` key has it dropped like any other non-whitelisted
    key: the proposal may not name the paths it is allowed to edit.
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
        allow=parse_allow(allow),
        prose_ignored=tuple(sorted(k for k in record if k not in RECORD_KEYS)),
    )
    return Proposal(
        candidate=candidate,
        mode=record.get("mode"),
        fault=record.get("fault"),
        tests=record.get("tests"),
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

    `--no-renames` so a move is reported under BOTH of its paths. Rename
    detection on would list only the destination, and a file moved out of the
    operator grant, the fence or the kernel would look like an addition
    somewhere else. The measured set is the one those checks are made against,
    so neither half of a move can be hidden.
    """
    tree = proposal.tree
    if not (tree / ".git").exists():
        return False, f"{tree} is not a git tree, so its diff cannot be measured"
    ok, out = _git(tree, ["diff", "--no-renames", "--name-only", proposal.base])
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

    Three named refusals and no third shape. A stage with no record at all was
    SKIPPED; a stage whose record carries no evidence is UNSUPPORTED; a stage
    whose record carries a `protocol_digest` that differs from this checkout's
    was produced by a DIFFERENT evaluator. The second is the one the item warns
    about: a controller that promotes when a stage's evidence is MISSING (rather
    than failing) is the fail-open shape, and this repository has measured ten
    separate checks that ran on every PR and could not fail. The third is issue
    #2802's: a record that names ANOTHER protocol is not evidence about this
    one. An absent digest is read rather than invented as a mismatch, because
    the lifecycle also judges hand-written records; a digest that is PRESENT
    and not this checkout's, `null` included, is a mismatch. The verdict the
    record feeds carries the protocol this checkout actually ran.
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
    own = own_protocol_digest()
    if "protocol_digest" in record:
        supplied = record["protocol_digest"]
        if supplied != own:
            return False, failed(
                stage, "PROTOCOL_MISMATCH",
                f"the `{stage}` record was produced by a different evaluator: it "
                f"carries protocol digest {supplied!r} (`null` means its scorer "
                f"could not name its own protocol), and this checkout's is "
                f"{own}. Two rounds scored by different protocols "
                f"cannot show they were scored by the same one, so the record is "
                f"refused rather than read",
                (f"record-protocol={supplied!r}",
                 f"checkout-protocol={own}"))
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
    `protocol_digest` is carried through only when the scorecard HAS the key,
    because the mismatch check distinguishes an absent key (a hand-written
    record) from a present `null` (a scorer that could not name its protocol).
    """
    if "retained" not in record:
        return record
    components = record.get("components") or []
    names = [c.get("component") for c in components if isinstance(c, dict)]
    blockers = list(record.get("blockers") or [])
    normalised = {
        "verified": bool(record.get("retained")),
        "reason": ("every reward component verified" if record.get("retained")
                   else "reward components did not verify: " + ", ".join(blockers)),
        "evidence": [f"tools/evolution_reward.py: {n}" for n in names if n],
        "diff": record.get("diff"),
    }
    # Copied only when the scorecard HAS the key. A scorecard that omits it is a
    # hand-written record, and `_record_for` reads it; a scorecard that carries
    # `null` is a scorer which could not name its protocol, and that is a
    # mismatch rather than an absence.
    if "protocol_digest" in record:
        normalised["protocol_digest"] = record["protocol_digest"]
    return normalised


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


def _judge_citation(proposal: Proposal) -> Verdict | None:
    """`None` when the proposal's mode citation is present, or its refusal.

    This is the check the roadmap states as a rule rather than a label. The
    controller cannot verify that a "repair" really repaired a localized fault
    or that a "feature" really implements the gap it names, so it does not
    pretend to. It checks the one thing a caller must hand over before any
    measured evidence can matter: a citation of the thing the mode is for. A
    missing citation is a named refusal, not a warning.
    """
    if proposal.mode == "repair":
        fault = proposal.fault
        if not isinstance(fault, str) or not fault.strip():
            return failed(
                "propose", "FAULT_NOT_CITED",
                "a `repair` proposal must cite the localized fault it fixes: "
                "the failing test or trace id the trace already localized. A "
                "repair with no diagnosis is a change with no fault, which is "
                "not the work item 520's repair mode is for")
        return None
    if proposal.mode == "feature":
        tests = proposal.tests
        if (not isinstance(tests, (list, tuple)) or not tests
                or not all(isinstance(t, str) and t.strip() for t in tests)):
            return failed(
                "propose", "TESTS_NOT_CITED",
                "a `feature` proposal must cite the tests the declared gap "
                "names (`tests`, a non-empty list of test selectors). Without "
                "them the gap is a wish, not a declared gap against the tests "
                "it names")
        return None

    # Evolution mode. It may produce a proposal and nothing else, so any
    # record for a stage past `propose` is a call asking the lifecycle to walk
    # through a door this mode does not have. Refusing here keeps that check on
    # the same precondition that carries the mode, before `admit` is reached.
    beyond = sorted(s for s in proposal.stages
                    if s not in ("trigger", "propose"))
    if beyond:
        return failed(
            "propose", "MODE_REFUSES_ACTIVATION",
            "the proposal is in `evolution` mode, which produces a proposal "
            "only: it may not merge, admit, activate or deploy. The record "
            "supplies evidence for " + ", ".join(f"`{s}`" for s in beyond)
            + ", so it asks the lifecycle to walk past the proposal. Refused "
              "with the mode named",
            (f"mode={proposal.mode}", f"stages={';'.join(beyond)}"))
    return None


def judge_propose(proposal: Proposal) -> Verdict:
    """The mode and its citation, the scope, and the TERMINATION obligation.

    Item 520 splits the work this lifecycle governs into three modes, and the
    mode is a field the controller reads rather than a convention it assumes.
    It is checked FIRST, before the stage record or the budget, because a
    proposal that has not said which mode it is cannot be judged by any later
    check: the three modes do not have the same citation and do not reach the
    same stages.

    Item 520 also says a proposal that cannot reach a verdict halts rather than
    loops. That is a proof obligation about the loop, and the checkable
    projection of it onto one proposal is the retry budget. A proposal with no
    declared budget is the unbounded case and fails closed; a proposal past its
    budget HALTS with a named code instead of being handed back to the
    proposer.
    """
    mode = proposal.mode
    if mode is None:
        return failed(
            "propose", "MODE_MISSING",
            "the proposal declares no `mode`. Item 520 splits the work this "
            "lifecycle governs into `repair`, `feature` and `evolution`, and "
            "the mode decides which path the proposal may take. It has no "
            "default, so a proposal that does not say which it is cannot be "
            "judged")
    if mode not in MODES:
        return failed(
            "propose", "MODE_UNKNOWN",
            f"the proposal declares mode {mode!r}, not one of "
            + ", ".join(f"`{m}`" for m in MODES)
            + ". A mode the controller does not know is not evidence of a "
              "path, so it cannot choose one",
            (f"mode={mode!r}",))
    citation = _judge_citation(proposal)
    if citation is not None:
        return citation
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

    Five checks, every one of them computed by the controller rather than read
    off the record, because a proposal that can hand in a green authority
    record and be believed has not had its authority checked.

      1. THE DIFF IS MEASURED, not declared. When the tree is a git tree the
         changed-file set comes from git and wins. A declared set that
         understates it is `DIFF_DISAGREES`; a tree that cannot be read at all
         is `DIFF_UNVERIFIED` and cannot promote. A self-reported diff is the
         candidate's account of itself, which is the one input this lifecycle
         does not accept.
      2. THE OPERATOR ALLOW SET. The editable set belongs to whoever starts the
         run, not to the proposal (#2803). No grant is `NO_ALLOW_SET`; a
         measured path outside it is `OUTSIDE_ALLOW`. Both fire here, before
         shadow or canary can be read, so a diff outside the grant never gets
         as far as a measurement.
      3. THE FENCE. A diff reaching this module or its tests is refused: the
         proposal would be judged by rules it just edited. This is the
         invariant turned into a check.
      4. THE KERNEL. A diff reaching `KERNEL_PATHS` is refused by name, citing
         the guarantee it defends, per issue #1223's exit test.
      5. THE AXES. A non-empty capability, taint, budget, realm or retention
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
                (f"git diff --no-renames --name-only {proposal.base}",))

    allow = proposal.candidate.allow
    if not allow:
        return failed(
            "authority", "NO_ALLOW_SET",
            f"no operator allow set was supplied (--allow or {ALLOW_ENV}), so "
            "nothing names what this proposal may edit. The editable set "
            "belongs to whoever starts the run, and default deny is not a "
            "pass",
            (f"tree={proposal.tree}",))
    outside = [p for p in measured if not _in_scope(p, allow)]
    if outside:
        return failed(
            "authority", "OUTSIDE_ALLOW",
            f"{len(outside)} measured path(s) outside the operator allow set "
            f"({';'.join(allow)}): " + ", ".join(outside[:6])
            + ". The measured stages are not read for a diff outside the "
              "operator's grant",
            (f"git diff --no-renames --name-only {proposal.base}",
             "allow=" + ";".join(allow)))

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
        f"{len(measured)} measured path(s), all inside the operator allow set "
        f"and none in the fence or the kernel",
        base.evidence + (f"git diff --no-renames --name-only {proposal.base}",
                         "allow=" + ";".join(allow),
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
    #: This checkout's evaluator identity, or None when even that could not be
    #: read. It is a field rather than a module read so the artifact keeps the
    #: protocol the verdict was actually produced under (issue #2802).
    protocol: str | None = None

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
            "protocol_digest": self.protocol,
            "stages": [v.as_dict() for v in self.verdicts],
            "preconditions": list(PRECONDITIONS),
            "measured": list(MEASURED),
            "supplied_by": dict(SUPPLIED_BY),
            "mode": self.proposal.mode,
            "fault": self.proposal.fault,
            "tests": (list(self.proposal.tests)
                      if isinstance(self.proposal.tests, (list, tuple))
                      else self.proposal.tests),
            "base": self.proposal.base,
            "scope": list(self.proposal.candidate.scope),
            "allow": list(self.proposal.candidate.allow),
            "attempt": self.proposal.attempt,
            "budget": self.proposal.budget,
            "prose_ignored": list(self.proposal.candidate.prose_ignored),
        }

    def render(self) -> str:
        lines = [f"evolution controller over {self.proposal.tree}",
                 f"base {self.proposal.base}",
                 f"mode {self.proposal.mode}",
                 f"protocol {self.protocol or 'unavailable'}", ""]
        for v in self.verdicts:
            kind = "precondition" if v.component in PRECONDITIONS else "measured"
            mark = "ok  " if v.verified else "FAIL"
            lines.append(f"  {mark} {v.component:<10} [{kind:<12}] {v.reason}")
        lines.append("")
        if not self.measurements_read:
            if self.decision == PROPOSAL_RECORDED:
                lines.append(
                    "the measured stages were NOT read: evolution mode ends at "
                    "the proposal, so no measured stage was reached.")
            else:
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
        elif self.decision == PROPOSAL_RECORDED:
            lines.append(
                "PROPOSAL RECORDED (evolution mode): the proposal is recorded "
                "and nothing past it was reached. Evolution mode may not "
                "merge, admit, activate or deploy.")
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

    A verified `propose` for an `evolution` proposal stops here as well, and
    for the same reason: the mode's product is the proposal, so there is no
    code path from it to `admit`, `authority` or a measured stage. The stages it
    did not reach are recorded `NOT_REACHED`, so the artifact says the stop was
    the mode and not a failed precondition.

    The checkout's protocol identity is read HERE, once, so a controller that
    cannot name its own evaluator refuses with a code instead of failing to
    import, and two verdicts from one process each compare against the protocol
    they are about to run.
    """
    global _OWN_PROTOCOL
    _OWN_PROTOCOL = None
    try:
        own = own_protocol_digest()
    except (OSError, RuntimeError) as exc:
        return LifecycleVerdict(
            proposal, [], "REFUSE", "PROTOCOL_UNAVAILABLE",
            measurements_read=False,
            findings=[f"the controller cannot identify its own evaluator, so it "
                      f"cannot compare a stage record against it: {exc}"])

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
                measurements_read=False, findings=findings, protocol=own)

        if stage == "propose" and proposal.mode == "evolution":
            # The mode stop, and the whole of the enforcement: it is control
            # flow, not a check on a field. The record has already been refused
            # if it supplied a stage past `propose` (`MODE_REFUSES_ACTIVATION`),
            # so reaching this line means the proposal asked for nothing past
            # itself. Every later stage is recorded as not reached and the
            # lifecycle ends without reading a single measurement.
            for rest in STAGES[STAGES.index(stage) + 1:]:
                verdicts.append(recorded_only(rest, proposal.mode))
            return LifecycleVerdict(
                proposal, verdicts, PROPOSAL_RECORDED, "",
                measurements_read=False, protocol=own)

    # Only now. Every precondition is satisfied, so the candidate was entitled
    # to enter shadow, and the measurements decide keep versus undo.
    #
    # The measured stages are walked in their own order and stop at the first
    # failure, for the same reason the preconditions do: nothing canaries a
    # candidate that failed shadow.
    findings = []
    first_failure = None
    unread = []
    for stage in MEASURED:
        if first_failure is not None:
            verdicts.append(not_reached(stage, first_failure.component))
            unread.append(stage)
            continue
        verdict = _judge(proposal, stage)
        verdicts.append(verdict)
        if not verdict.verified:
            first_failure = verdict
    if first_failure is None:
        return LifecycleVerdict(proposal, verdicts, "PROMOTE", "",
                                measurements_read=True, protocol=own)
    supplied_anyway = [s for s in unread if s in proposal.stages]
    if supplied_anyway:
        findings.append(
            "OUT_OF_ORDER: " + ", ".join(supplied_anyway) + " evidence was "
            "supplied although `" + first_failure.component + "` did not "
            "verify.")

    # ROLL_BACK versus REFUSE turns on one fact and not on severity: did the
    # candidate go live. `canary` is the stage that puts it in front of real
    # traffic, so a canary record is the evidence that there is something to
    # undo. Without one nothing was activated, and calling that a rollback
    # would claim an undo that never ran.
    activated = "canary" in proposal.stages
    findings.insert(0, f"`{first_failure.component}` did not verify "
                       f"({first_failure.code})")
    return LifecycleVerdict(
        proposal, verdicts,
        "ROLL_BACK" if activated else "REFUSE", first_failure.code,
        measurements_read=True, findings=findings, protocol=own)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--proposal", type=Path, required=True,
                    help="JSON record: tree/base/scope/mode plus the mode's "
                         "citation, trigger, granted, attempt, budget and the "
                         "per-stage evidence")
    ap.add_argument("--json", type=Path, help="write the verdict here")
    ap.add_argument("--allow", action="append", metavar="GLOB", default=None,
                    help="a path glob the operator allows the proposal to "
                         "edit; repeatable or comma-separated, and falls back "
                         f"to {ALLOW_ENV}. No allow set refuses at `authority`")
    args = ap.parse_args(argv)
    allow = (parse_allow(args.allow) if args.allow is not None
             else allow_from_env())

    try:
        record = json.loads(args.proposal.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"evolution_controller: cannot read {args.proposal}: {exc}",
              file=sys.stderr)
        return REFUSE
    try:
        proposal = load_proposal(record, allow=allow)
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

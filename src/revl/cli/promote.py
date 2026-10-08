"""`revl promote` — decide a promotion from RECORDED evidence.

Issue #1192's gate (`revl.shadow_promotion`) and its schedule
(`revl.shadow_routing`) could be run only from inside a test: the declaration a
verdict is judged against and the window it is judged on lived in the test's
locals. This verb is what makes the decision reachable by an OPERATOR, from two
artifacts:

* a PLAN document — the declaration (`revl.shadow-plan`), written by
  `ShadowPlan.as_dict` and read back by `promotion.plan_from_dict`;
* a WINDOW document — the accumulated evidence (`revl.shadow-window`), written
  by `ShadowLedger.as_window` and read back by `routing.window_from_dict`.

It adds NO decision logic. It reads the two documents, resolves the recorded
schedule against the two compositions the window was taken over, attaches item
496's two recorded worlds (derived, not carried — see
`docs/design/558-shadow-scheduling.md`), and calls the same
composition -> schedule -> gate walk the tests call. It decides; it does not
land the promotion. `revl.shadow_register` is not imported here at all: the
verb that decides is not the verb that moves.

Exit status is the verdict and nothing else: 0 only on a `PROMOTE` whose
comparison ran, 1 on every `REFUSE` and every `REVERT`. A `REFUSE` is a
decision, not a crash — so a refusal prints the named link it stopped at.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace


def _read_document(path, what: str):
    """``(document, failure)`` from one JSON artifact: one of the two is None.

    A file that is not there, is not JSON, or is not an object is an INPUT
    failure and not a refusal: it never reaches a verdict, because there is
    nothing yet to decide. That is the one place in this verb where "no
    answer" is the honest answer."""
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        return (None, f"cannot read {path}: {error}")
    if not isinstance(document, dict):
        return (None, f"{path}: expected a {what} document (an object), "
                      f"got {type(document).__name__}")
    return (document, None)


def _refused(plan, link: str, reason: str, stage: str) -> int:
    """Print a `REFUSE` naming ``link`` and return the refusal exit status.

    Built by `promotion.refused`, which is the gate's own constructor for a
    precondition a CALLER owns, so this verb cannot invent a link, a stage or a
    fourth decision word: the refusal is the same object a refusal inside
    `decide` is, reported at the stage this caller checked."""
    from .. import shadow_promotion as promotion

    verdict = promotion.refused(plan, link, reason, stage=stage)
    print(promotion.render(verdict))
    return 1


def _unresolved(ledger, generations):
    """``(link, reason, stage)`` for the first generation the recorded schedule
    does not resolve against, or ``None``.

    `revl.shadow_runtime.decide` resolves the incumbent itself, so the
    incumbent's half is checked twice; that is not a second rule, it is the
    same rule applied to the second input. The candidate's half is the one
    `decide` cannot make, and it has to be made BEFORE any world is built:
    `canary.slice_timeline` raises when a generation does not declare the
    component, and a comparison that could not run must be reported rather
    than skipped, because a skipped comparison reads exactly like a clean one.
    """
    from .. import shadow_runtime as runtime

    for name, ir in generations:
        resolution = runtime.resolve(ir, ledger.route)
        if not resolution.ok:
            link, reason = resolution.refusal
            return (link, f"{name}: {reason}", runtime.COMPOSITION_STAGE)
    return None


def _with_recorded_worlds(entries, running, successor):
    """The window's entries, each carrying item 496's two RECORDED WORLDS.

    The worlds are item 496's own construction, reached through
    `revl.shadow_runtime.world_for` — which is `canary.slice_timeline`, called
    and not reimplemented — over the two generations this window was taken
    over. They are DERIVED here rather than read out of the document, for the
    reason `ShadowLedger.as_window` gives: the candidate's own completion
    crosses the same seam and mints a step index, so a run's timeline is not
    step-for-step comparable with another generation's static walk.

    Each entry's `Observation` is rebuilt rather than mutated: `Observation` is
    what carries the worlds, its `slo_reads` counter is per object, and a fresh
    object is the only way to hand the gate a window whose counter counts
    reads on THIS decision.
    """
    from .. import shadow_promotion as promotion
    from .. import shadow_routing as routing
    from .. import shadow_runtime as runtime

    component = entries[0].component if entries else None
    left = runtime.world_for(running, component)
    right = runtime.world_for(successor, component)
    return tuple(
        routing.Entry(
            crossing=entry.crossing, component=entry.component,
            action=entry.action, realm=entry.realm, side=entry.side,
            observation=promotion.Observation(
                incumbent=entry.observation.incumbent,
                candidate=entry.observation.candidate,
                slo=entry.observation.slo_supplied,
                realm=entry.observation.realm,
                incumbent_world=left, candidate_world=right))
        for entry in entries)


def _run_promote(args) -> int:
    """`revl promote <files> --candidate FILE --plan PLAN.json --window
    WINDOW.json` — decide a promotion from recorded evidence (issue #1192,
    docs/design/558-shadow-scheduling.md).

    The verdict is a REPLAY COMPARISON and never a metric threshold: the gate
    reads no latency and no cost, and this verb supplies none. It is also an
    ADMISSION DECISION and not a numeric flip — there is no expression in the
    pipeline it runs in which an agreement figure and a metric appear
    together.

    Exit status: 0 only on a `PROMOTE` whose comparison ran. A `REFUSE` (the
    evidence is not evidence, the sample is too small, agreement is below the
    stated threshold, a document is malformed) and a `REVERT` (a LIVE window
    whose first attributed divergence names a crossing) both exit 1. `revl
    promote` decides; landing the promotion is the register's job, and this
    verb does not touch it."""
    from .. import shadow_promotion as promotion
    from .. import shadow_routing as routing
    from .. import shadow_runtime as runtime
    from ..compiler import compile_files
    from ..errors import RevlError
    from ..model_evidence import resolve_key

    window, failure = _read_document(args.window, "window")
    if failure is not None:
        print(f"error: {failure}", file=sys.stderr)
        return 1
    plan_document, failure = _read_document(args.plan, "plan")
    if failure is not None:
        print(f"error: {failure}", file=sys.stderr)
        return 1

    # A malformed document is not a promotion and does not get a verdict: the
    # gate cannot be asked about a declaration that did not parse, and
    # defaulting a member is how a promotion comes to rest on a window nobody
    # recorded. The reader names which of the two mistakes it is.
    ledger, refusal = routing.window_from_dict(window)
    if refusal is not None:
        print(f"error: {args.window}: {refusal[0]}: {refusal[1]}",
              file=sys.stderr)
        return 1
    plan, refusal = promotion.plan_from_dict(plan_document)
    if refusal is not None:
        print(f"error: {args.plan}: {refusal.link}: {refusal.reason}",
              file=sys.stderr)
        return 1

    try:
        running = compile_files(list(args.files))
        successor = compile_files(list(args.candidate))
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    refused = _unresolved(
        ledger, (("the incumbent composition", running),
                 ("the candidate composition", successor)))
    if refused is not None:
        return _refused(plan, refused[0], refused[1], refused[2])

    worlded = replace(ledger, entries=_with_recorded_worlds(
        ledger.entries, running, successor))

    # The evidence key is optional and its ABSENCE is fail-closed rather than
    # fail-open: `admit` refuses a record it cannot verify, so a promotion run
    # without a key is a `REFUSE` naming `evidence-unverifiable`, never a
    # `PROMOTE` on an unchecked seal. The verifier itself is left to the
    # gate's own lazy resolution for the same reason.
    verdict = runtime.decide(
        running, worlded.route, plan, worlded.entries, ledger=worlded,
        key=resolve_key(args.evidence_key))

    print(json.dumps(verdict.as_dict(), indent=2) if args.json
          else routing.render(verdict, worlded))
    return 0 if verdict.decision == promotion.PROMOTE else 1

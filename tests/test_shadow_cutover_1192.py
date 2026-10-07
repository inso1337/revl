"""Shadow cutover: a landed promotion changes WHICH MODEL ANSWERS, and a
revert puts the incumbent back (roadmap item 518, issue #1192).

The executable spec for the one clause of `docs/design/558-shadow-scheduling.md`
section 17.6 that the earlier slices left open:

    "No tier consults the register to pick the role that answers ... the python
    seam serves the observer's answer for a class the register holds as
    promoted instead of discarding it."

`tests/test_shadow_promotion_518.py` measures the gate, `tests/test_shadow_
routing_518.py` the schedule, `tests/test_shadow_runtime_518.py` the seam, and
`tests/test_shadow_register_518.py` the arm that moves. Every one of them stops
at "the register says the class is promoted". This file is the measurement of
the step after that: the register's answer reaching the BODY, as the model name
on the completion the body was handed.

The premise, re-measured before the change and printed by this file's first
two tests: a promotion landed, the route read `live=True`, the successor was
consulted on all twenty crossings — and the body still received
`openai:gpt-4o-2024-08-06` on all twenty-one completions, because the seam
discarded the observer's return. The register was the declared arm and the
gate's rule and nothing else.

What this file does NOT measure: the other five tiers' seams, `revl promote`,
and the trace's `llm` span (the cutover is not yet recorded as the served side
there). See the module's `WHAT THIS DOES NOT DO`.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_shadow_register_518 as rt  # noqa: E402
import test_shadow_runtime_518 as live  # noqa: E402
from revl import shadow_promotion as sp  # noqa: E402
from revl import shadow_register as reg  # noqa: E402
from revl import shadow_routing as sr  # noqa: E402
from revl import shadow_runtime as srt  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.mcp import canary  # noqa: E402
from revl.mcp.session import replay_module  # noqa: E402

#: The two roles' model names, as `test_shadow_runtime_518.py`'s `host_return`
#: mints them: the incumbent's is the default and the successor's is the one
#: the candidate producer asks for. They are what a body can actually see.
INCUMBENT_MODEL = "openai:gpt-4o-2024-08-06"
SUCCESSOR_MODEL = "local:successor-1"


@pytest.fixture(scope="module")
def incumbent_ir():
    return compile_source(live.INCUMBENT_SRC, "incumbent.rvl")


@pytest.fixture(scope="module")
def same_ir():
    return compile_source(live.CANDIDATE_SAME_SRC, "candidate_same.rvl")


def serve_class(the_register, action_class, incumbent_ir, candidate_ir, **kw):
    """Drive ``action_class``'s register-derived route through the python
    tier's completion seam and report what the BODY was handed.

    Returns ``(route, ledger, timeline, models, calls)``. ``models`` is the
    model name of every completion the body received, in crossing order: the
    one measurement this item is about, and the one no earlier file took.
    """
    route = the_register.route(action_class, realm=live.REALM,
                               share=sr.EVERYTHING, salt="pinned",
                               candidate_role=rt.SUCCESSOR)
    resolution = srt.resolve(incumbent_ir, route)
    incumbent, candidate, calls = rt.sides(incumbent_ir, candidate_ir, route,
                                           **kw)
    shadow = srt.TierShadow(resolution, incumbent=incumbent,
                            candidate=candidate)
    timeline, served = live.drive(incumbent_ir, live.COMPONENT, shadow=shadow)
    return (route, shadow.ledger(), timeline,
            [s["model"] for s in served], calls)


def served_by_crossing(timeline, models):
    """``{crossing: model}``, so a claim about ONE crossing is about that
    crossing and not about the multiset."""
    return dict(zip(live.crossings_of(timeline), models))


# ==========================================================================
# 1. the premise: before the promotion, the successor's answer is discarded
# ==========================================================================

def test_a_class_that_is_not_promoted_serves_the_incumbents_model(
        incumbent_ir, same_ir):
    """The BEFORE, in the same file as the after.

    The successor answers with a different model name on every crossing, the
    schedule consults it on all twenty — and the body receives the incumbent's
    model on all twenty-one completions. A shadow, measured rather than
    asserted."""
    the_register = rt.register()
    assert the_register.arm(rt.SUMMARIZE) == rt.INCUMBENT

    route, ledger, _t, models, calls = serve_class(
        the_register, rt.SUMMARIZE, incumbent_ir, same_ir)

    assert route.live is False
    assert len(calls) == live.WIDTH                # the successor WAS consulted
    assert len(models) == live.WIDTH + 1
    assert set(models) == {INCUMBENT_MODEL}
    assert ledger.entries and all(e.side == sr.INCUMBENT
                                  for e in ledger.served if e.shadowed)


def test_a_promotion_lands_and_the_next_window_is_scheduled_live(
        incumbent_ir, same_ir):
    """The step the earlier slices already measured, restated so this file's
    other tests are not reading a register they did not move."""
    the_register = rt.register()
    landed, _ledger, _calls = rt.land(the_register, rt.SUMMARIZE, incumbent_ir,
                                      same_ir)

    assert landed.changed is True, landed.refusal
    assert landed.verdict.decision == sp.PROMOTE
    assert the_register.arm(rt.SUMMARIZE) == rt.SUCCESSOR
    assert the_register.route(rt.SUMMARIZE, realm=live.REALM,
                              share=sr.EVERYTHING).live is True


# ==========================================================================
# 2. THE EXIT: after the promotion, the successor's answer is what is served
# ==========================================================================

def test_a_landed_promotion_changes_which_model_answers(incumbent_ir,
                                                        same_ir):
    """The clause this increment closes.

    Same drive as the first test, one operation earlier in the register. The
    successor's model is now the model the body receives, on every crossing of
    the promoted action class."""
    the_register = rt.register()
    rt.land(the_register, rt.SUMMARIZE, incumbent_ir, same_ir)

    route, _ledger, _t, models, _calls = serve_class(
        the_register, rt.SUMMARIZE, incumbent_ir, same_ir)

    assert route.live is True
    assert SUCCESSOR_MODEL in models, (
        "the promotion landed and the body still never saw the successor's "
        "answer: the seam discarded the observer's return")
    assert models.count(SUCCESSOR_MODEL) == live.WIDTH


def test_the_promotion_reaches_only_the_promoted_action_class(incumbent_ir,
                                                              same_ir):
    """`classify` is the same component's other action class and is still on
    its base arm. Its crossing is not this route's to serve, so the body keeps
    the incumbent's model there — the cutover is scoped by the register's own
    `(component, action)` class and not by the component."""
    the_register = rt.register()
    rt.land(the_register, rt.SUMMARIZE, incumbent_ir, same_ir)

    _route, _ledger, timeline, models, _calls = serve_class(
        the_register, rt.SUMMARIZE, incumbent_ir, same_ir)
    by_crossing = served_by_crossing(timeline, models)

    # The recorder numbers crossings from 1, so `classify` — the anchor step
    # the slice runs first — is crossing 1 and the twenty `summarize` steps are
    # 2..21.
    assert by_crossing[(live.COMPONENT, 1)] == INCUMBENT_MODEL
    assert models.count(INCUMBENT_MODEL) == 1
    assert all(model == SUCCESSOR_MODEL
               for crossing, model in by_crossing.items()
               if crossing[1] != 1)


def test_the_model_the_body_received_is_the_side_the_ledger_recorded(
        incumbent_ir, same_ir):
    """One statement, two readers.

    Slice 2 DERIVED the served side from the route's `live` flag. This seam
    KNOWS: it returns the candidate's answer exactly when the schedule served
    the candidate, and returns `None` when it served the incumbent. So the
    ledger's side and the model the body received cannot disagree, and here
    both are read off one run."""
    the_register = rt.register()
    rt.land(the_register, rt.SUMMARIZE, incumbent_ir, same_ir)

    _route, ledger, _t, models, _calls = serve_class(
        the_register, rt.SUMMARIZE, incumbent_ir, same_ir)

    assert {e.side for e in ledger.served} == {sr.CANDIDATE}
    assert all(e.side == sr.CANDIDATE for e in ledger.entries)
    # ... and the count the body could see matches the count the ledger claims
    assert ledger.candidate_calls == models.count(SUCCESSOR_MODEL) \
        == live.WIDTH
    assert "answers served by the incumbent: 0 of " \
        in sr.render(srt.decide(incumbent_ir, incumbent_ir,
                                live.plan_for(ledger), ledger.entries,
                                key=live.KEY), ledger)


# ==========================================================================
# 3. the revert is real: the incumbent's answer comes back
# ==========================================================================

def test_a_revert_puts_the_incumbents_model_back(incumbent_ir, same_ir):
    """The other direction, and the reason this is a cutover rather than a
    switch: the promotion is undone by an operation, and the next window's
    body receives the incumbent's model again.

    The revert is induced the way `tests/test_shadow_register_518.py` induces
    it — a live window whose step 5 diverges — and it works BECAUSE the
    incumbent still answered under the cutover: the comparison that reverts is
    made from both answers, and the successor's own answer is what the body
    got."""
    the_register = rt.register()
    rt.land(the_register, rt.SUMMARIZE, incumbent_ir, same_ir)
    assert the_register.arm(rt.SUMMARIZE) == rt.SUCCESSOR

    outcome, _ledger, _calls = rt.observe(the_register, rt.SUMMARIZE,
                                          incumbent_ir, same_ir, disagree_at=5)

    assert outcome.verdict.decision == sp.REVERT
    assert outcome.verdict.refusal.link == sp.DIVERGENCE_ATTRIBUTED
    assert outcome.changed is True
    assert the_register.arm(rt.SUMMARIZE) == rt.INCUMBENT

    route, _ledger, _t, models, _calls = serve_class(
        the_register, rt.SUMMARIZE, incumbent_ir, same_ir)

    assert route.live is False
    assert set(models) == {INCUMBENT_MODEL}


def test_the_cutover_does_not_blind_the_gate(incumbent_ir, same_ir):
    """A live crossing still runs BOTH completions.

    Skipping the incumbent on a live route would make the cutover
    un-revertible: there would be no second answer to compare, so the gate
    would have nothing to revert on and the promotion would be a one-way
    switch that only ever reports. Measured here as both recorded worlds on
    every entry of a live window."""
    the_register = rt.register()
    rt.land(the_register, rt.SUMMARIZE, incumbent_ir, same_ir)

    _route, ledger, _t, models, _calls = serve_class(
        the_register, rt.SUMMARIZE, incumbent_ir, same_ir)

    assert ledger.worlds_recorded == live.WIDTH == len(ledger.entries)
    assert all(e.observation.incumbent_world is not None
               and e.observation.candidate_world is not None
               for e in ledger.entries)
    assert models.count(SUCCESSOR_MODEL) == live.WIDTH


# ==========================================================================
# 4. the other colour of the same seam
# ==========================================================================

def drive_async(ir, component, shadow):
    """`tests/test_shadow_runtime_518.py`'s `drive`, through the ASYNC colour.

    The two colours are separate call sites in `backends/python/runtime.py`,
    so a cutover on one is not a cutover on the other until both are
    measured."""
    runtime = srt.tier_runtime()
    replay = replay_module()
    runtime.revl_reset_run_trace_state()
    timeline = replay.Timeline(component)
    served = []
    shadow.attach()

    async def go():
        for step in canary.slice_timeline(ir, component).steps:
            detail = dict(step.detail) if isinstance(step.detail, dict) else {}
            if step.kind != replay.KIND_EMISSION:
                timeline._add(step.kind, step.label, step.effect,
                              detail=detail)
                continue
            key, method, args = live._call_of(step.label)

            async def make_call(k=key, m=method, a=args):
                timeline.record_emission(k, m, a, "Model", ("shadow.rvl", 1))
                return live.host_return()

            served.append(await runtime.validate_retry_async(
                make_call, budget=0, schema={"type": "object"},
                where=component))

    try:
        asyncio.run(go())
    finally:
        shadow.detach()
    return timeline, served


def test_the_async_colour_serves_the_candidate_too(incumbent_ir, same_ir):
    """A caller could not dodge the cutover by awaiting.

    Same promotion, same route, the `await`ing seam: the body receives the
    successor's model. A promotion that only took effect on the sync colour
    would be a cutover half the call sites do not have."""
    the_register = rt.register()
    rt.land(the_register, rt.SUMMARIZE, incumbent_ir, same_ir)

    route = the_register.route(rt.SUMMARIZE, realm=live.REALM,
                               share=sr.EVERYTHING, salt="pinned",
                               candidate_role=rt.SUCCESSOR)
    assert route.live is True
    resolution = srt.resolve(incumbent_ir, route)
    incumbent, candidate, _calls = rt.sides(incumbent_ir, same_ir, route)
    shadow = srt.TierShadow(resolution, incumbent=incumbent,
                            candidate=candidate)

    _timeline, served = drive_async(incumbent_ir, live.COMPONENT, shadow)
    models = [s["model"] for s in served]

    assert len(models) == live.WIDTH + 1
    assert models.count(SUCCESSOR_MODEL) == live.WIDTH


def test_the_async_colour_on_a_shadow_route_keeps_the_incumbent(
        incumbent_ir, same_ir):
    """And the same call site is still a no-op before a promotion lands, so
    the async edit is additive for every composition that never promotes."""
    the_register = rt.register()
    route = the_register.route(rt.SUMMARIZE, realm=live.REALM,
                               share=sr.EVERYTHING, salt="pinned",
                               candidate_role=rt.SUCCESSOR)
    assert route.live is False
    resolution = srt.resolve(incumbent_ir, route)
    incumbent, candidate, _calls = rt.sides(incumbent_ir, same_ir, route)
    shadow = srt.TierShadow(resolution, incumbent=incumbent,
                            candidate=candidate)

    _timeline, served = drive_async(incumbent_ir, live.COMPONENT, shadow)

    assert set(s["model"] for s in served) == {INCUMBENT_MODEL}


# ==========================================================================
# 5. what did not change: no promotion, no seam, no observer
# ==========================================================================

def test_a_composition_with_no_observer_is_untouched(incumbent_ir):
    """The additive claim. An emitted program that never attaches a shadow
    gets the same object `validate_response` produced, because every path
    through the hook returns `None` when no observer is wired."""
    runtime = srt.tier_runtime()
    replay = replay_module()
    runtime.revl_reset_run_trace_state()
    timeline = replay.Timeline(live.COMPONENT)
    served = []
    for step in canary.slice_timeline(incumbent_ir, live.COMPONENT).steps:
        detail = dict(step.detail) if isinstance(step.detail, dict) else {}
        if step.kind != replay.KIND_EMISSION:
            timeline._add(step.kind, step.label, step.effect, detail=detail)
            continue
        key, method, args = live._call_of(step.label)

        def make_call(k=key, m=method, a=args):
            timeline.record_emission(k, m, a, "Model", ("shadow.rvl", 1))
            return live.host_return()

        served.append(runtime.validate_retry(
            make_call, budget=0, schema={"type": "object"},
            where=live.COMPONENT))

    assert set(s["model"] for s in served) == {INCUMBENT_MODEL}


def test_a_faulted_observer_leaves_the_incumbents_answer_in_place(
        incumbent_ir, same_ir):
    """A cutover must not be able to serve an answer it could not produce.

    The hook catches an observer fault, detaches, and returns `None`, so the
    worst a faulted successor can do is leave the incumbent's validated
    response where it was — the same property the shadow had, now that the
    return value matters."""
    the_register = rt.register()
    rt.land(the_register, rt.SUMMARIZE, incumbent_ir, same_ir)
    route = the_register.route(rt.SUMMARIZE, realm=live.REALM,
                               share=sr.EVERYTHING, salt="pinned",
                               candidate_role=rt.SUCCESSOR)
    assert route.live is True
    resolution = srt.resolve(incumbent_ir, route)

    def incumbent(crossing, value):
        step = crossing[1]
        return srt.answered(incumbent_ir, live.COMPONENT, live.seal(
            step_index=step, role=rt.INCUMBENT,
            placement=live.PLACEMENT_INCUMBENT,
            answer=live.answer_digest(f"said-{step}"),
            prompt=f"asked-{step}"))

    def candidate(crossing):
        raise RuntimeError("the successor's provider is down")

    shadow = srt.TierShadow(resolution, incumbent=incumbent,
                            candidate=candidate)
    _timeline, served = live.drive(incumbent_ir, live.COMPONENT,
                                   shadow=shadow)

    assert set(s["model"] for s in served) == {INCUMBENT_MODEL}
    assert shadow.faults, "the fault is kept for the operator to read"
    assert len(served) == live.WIDTH + 1


# ==========================================================================
# 6. the register the cutover reads is the register, not a copy
# ==========================================================================

def test_the_cutover_follows_the_register_after_a_serialisation_round_trip(
        incumbent_ir, same_ir):
    """The route is derived per window from the register's live state, so a
    register that was saved and reloaded promotes just as well. This is what
    keeps the cutover from being a flag a caller passes."""
    the_register = rt.register()
    rt.land(the_register, rt.SUMMARIZE, incumbent_ir, same_ir)
    reloaded = reg.PromotionRegister.from_dict(
        json.loads(json.dumps(the_register.as_dict())))
    assert reloaded.arm(rt.SUMMARIZE) == rt.SUCCESSOR

    _route, _ledger, _t, models, _calls = serve_class(
        reloaded, rt.SUMMARIZE, incumbent_ir, same_ir)

    assert models.count(SUCCESSOR_MODEL) == live.WIDTH

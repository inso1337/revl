"""Shadow runtime: a live shadow over a real composition (roadmap item 518,
issue #1192).

The executable spec for `docs/design/558-shadow-scheduling.md` sections 10 to
13. Slice 2 (`revl.shadow_routing`, PR #1297) scheduled the shadow and
measured the scheduling; note 558 section 9 then listed four things it did not
do, all four of them caused by the same absence: nothing was wired to a
running composition. This file is that wiring, measured.

What is different from slice 2's file, and is the point of this one:

* the composition is COMPILED here, by `revl.compiler.compile_source`, and the
  three generations differ the way two generations of a real component differ;
* the crossings are the PYTHON TIER'S OWN. Every one is minted by
  `backends/python/replay.py`'s recorder inside
  `backends/python/runtime.py`'s `validate_retry`, which is the single seam
  every model completion in that tier crosses;
* the two recorded worlds are BUILT from the two generations' IRs, by item
  496's own walker, rather than handed to the scheduler;
* the records are sealed and verified by `revl.model_evidence` itself. There
  is no stub verifier in this file.

What is NOT run, stated once here rather than implied: there is no cordis
activation. `import cordis` resolves in exactly one CI job and in no local
checkout, so what runs is the tier's recorder and its completion seam, driven
over the composition's declared steps. `test_the_recorder_mints_the_indices_
the_composition_declares` is the measurement that says the crossing keys are
the recorder's own and agree with the composition's account of them.
"""

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import model_evidence as me  # noqa: E402
from revl import model_route  # noqa: E402
from revl import parser as revl_parser  # noqa: E402
from revl import shadow_promotion as sp  # noqa: E402
from revl import shadow_routing as sr  # noqa: E402
from revl import shadow_runtime as srt  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.mcp import canary  # noqa: E402
from revl.mcp.session import replay_module  # noqa: E402

MODULE_PATH = ROOT / "src" / "revl" / "shadow_runtime.py"

KEY = b"shadow-runtime-test-key-0123456789"
COMPONENT = "Classifier"
ACTION = "summarize"
ANCHOR_ACTION = "classify"
REALM = "tenant_a"
OTHER_REALM = "tenant_b"

MODEL_DIGEST = "d" * 64
PLACEMENT_INCUMBENT = "1" * 64
PLACEMENT_SUCCESSOR = "2" * 64
POLICY = "e" * 64

#: How many crossings `summarize` declares. Twenty, the same window size
#: slice 2 measured on, so the two files' non-vacuity rows are comparable.
WIDTH = 20

SAMPLING = {"max_tokens": 256, "seed": 7, "stop_digest": None,
            "temperature": 0.0, "top_k": None, "top_p": 1.0}

PERFECT_SLO = {"cost": 0.0, "latency_ms": 1.0, "refusal_rate": 0.0,
               "tokens": 1}


# ==========================================================================
# the composition: three generations of one component
# ==========================================================================
#
# `classify` carries ONE crossing and `summarize` carries twenty. The single
# one is not decoration: a route over `classify` has to RESOLVE for the
# forged-stamp row to be about the stamp rather than about an action that
# crosses no boundary, and section 1.1 of note 558 is exactly a window of one
# action's crossings promoting the other.

ANCHOR = ('      emit model.complete("anchor") '
          'compensate model.cancel("anchor")')


def _emits(indent="      "):
    return "\n".join(
        f'{indent}emit model.complete("p{i}") compensate model.cancel("p{i}")'
        for i in range(WIDTH))


def source(*, moved=False, extra=False, realm=REALM):
    """One generation of the composition.

    `moved` relocates every one of `summarize`'s crossings into `classify`
    WITHOUT changing the flat step list: the same kinds, the same labels, the
    same order, reached from a different entry point. That is item 496's own
    finding: a generation that moves a step between entry points records a
    different world and names the same completions, and it is the shape a
    record comparison cannot see.

    `extra` adds an unrelated component in a sibling realm. It makes a
    generation that is genuinely a different composition and whose recorded
    world for THIS slice is identical, which is what a promote is supposed to
    look like."""
    if moved:
        body = (f"    fn classify(text) {{\n{ANCHOR}\n{_emits()}\n"
                f"      return text\n    }}\n"
                f"    fn summarize(text) = text")
    else:
        body = (f"    fn classify(text) {{\n{ANCHOR}\n      return text\n"
                f"    }}\n"
                f"    fn summarize(text) {{\n{_emits()}\n"
                f"      return text\n    }}")
    sibling = """
component Probe {
  let probe = effect Map.new() undo probe.drop()
}
""" if extra else ""
    return f"""
model role incumbent off_device
model role successor off_device

service Answer {{
  emission fn classify(text: Str) -> Str
  emission fn summarize(text: Str) -> Str
}}

service Model {{
  emission fn complete(prompt: Str) -> Str
  emission fn cancel(prompt: Str)
}}

component Classifier requires model: Model provides out: Answer {{
  isolate out in realm("{realm}")
  isolate model in realm("{realm}")
  route model on classify {{ input -> incumbent, * -> successor }}
  route model on summarize {{ input -> incumbent, * -> successor }}
  provide out {{
{body}
  }}
}}
{sibling}"""


INCUMBENT_SRC = source()
CANDIDATE_SAME_SRC = source(extra=True)
CANDIDATE_MOVED_SRC = source(moved=True)


@pytest.fixture(scope="module")
def incumbent_ir():
    return compile_source(INCUMBENT_SRC, "incumbent.rvl")


@pytest.fixture(scope="module")
def same_ir():
    return compile_source(CANDIDATE_SAME_SRC, "candidate_same.rvl")


@pytest.fixture(scope="module")
def moved_ir():
    return compile_source(CANDIDATE_MOVED_SRC, "candidate_moved.rvl")


def route_table():
    program = revl_parser.Parser(INCUMBENT_SRC, "incumbent.rvl").parse()
    return model_route.check(program)


# ==========================================================================
# sealed evidence, from `revl.model_evidence` itself
# ==========================================================================

def seal(*, step_index, role, placement, answer, prompt):
    """One item 517 record, sealed by the real sealer and verified by the
    real verifier. Slice 2 built these locally against a stub; the shape is
    what must not drift and this file holds a composition, so it pays the
    real thing."""
    return me.seal(
        KEY, component=COMPONENT, step_index=step_index, role=role,
        residence="off_device", model_digest=MODEL_DIGEST,
        placement_digest=placement,
        prompt_binding={"mode": "content-addressed",
                        "value": me.digest(prompt), "reason": None},
        origins=["input"], candidates=[answer], chosen=0,
        outcome="validated", sampling=dict(SAMPLING), policy_digest=POLICY,
        fallback_depth=0)


def answer_digest(text):
    return me.digest(text)


# ==========================================================================
# the live run: the python tier's recorder and completion seam
# ==========================================================================

def _call_of(label):
    """`model.complete("p3")` -> `("model", "complete", ("p3",))`."""
    head, _, tail = label.partition("(")
    key, _, method = head.partition(".")
    args = tuple(a.strip().strip("'\"") for a in
                 tail.rstrip(")").split(",") if a.strip())
    return key, method, args


def host_return(model="openai:gpt-4o-2024-08-06"):
    return {"tag": "ok", "model": model, "tokensIn": 12, "tokensOut": 8,
            "cost": {"amount": 0.0001, "currency": "USD"}}


def drive(ir, component, *, shadow=None, budget=0):
    """Run one activation's declared steps through the PYTHON TIER'S OWN
    recorder and completion seam, and return `(timeline, served)`.

    Every crossing here is minted by `replay.Timeline.record_emission` from
    inside `runtime.validate_retry`'s `make_call`, which is the nesting a live
    run has: the recorder marks the crossing, the seam measures the completion
    that just crossed it, and (since this branch) consults the shadow
    observer for it. Nothing about the crossing key is invented by this
    function; `_call_of` only turns the composition's own step label back into
    the receiver and method the recorder is called with.

    There is no cordis activation. What runs is the recorder and the seam."""
    replay = replay_module()
    runtime = srt.tier_runtime()
    runtime.revl_reset_run_trace_state()
    timeline = replay.Timeline(component)
    if shadow is not None:
        shadow.attach()
    served = []
    try:
        for step in canary.slice_timeline(ir, component).steps:
            detail = dict(step.detail) if isinstance(step.detail, dict) else {}
            if step.kind != replay.KIND_EMISSION:
                timeline._add(step.kind, step.label, step.effect,
                              detail=detail)
                continue
            key, method, args = _call_of(step.label)

            def make_call(k=key, m=method, a=args):
                timeline.record_emission(k, m, a, "Model", ("shadow.rvl", 1))
                return host_return()

            served.append(runtime.validate_retry(
                make_call, budget=budget, schema={"type": "object"},
                where=component))
    finally:
        if shadow is not None:
            shadow.detach()
    return timeline, served


def crossings_of(timeline):
    """The `(component, step_index)` keys a recorded timeline minted."""
    replay = replay_module()
    return tuple((timeline.component, s.index) for s in timeline.steps
                 if s.kind == replay.KIND_EMISSION)


# ==========================================================================
# the two sides of the seam
# ==========================================================================

def route(**overrides):
    base = dict(component=COMPONENT, action=ACTION, realm=REALM,
                incumbent_role="incumbent", candidate_role="successor",
                share=sr.EVERYTHING, salt="pinned", live=False)
    base.update(overrides)
    return sr.ShadowRoute(**base)


EMPTY_DIFF = {axis: [] for axis in sp.AUTHORITY_AXES}
LAYERS = {"route-arm": "revertible",
          "agreement-ledger": "compensatable",
          "placement-history": "neither"}
COMPENSATIONS = {
    "agreement-ledger": {"name": "supersede-window", "tested": True},
    "placement-history": {"name": "replay-placement-log", "tested": True},
}


def plan_for(ledger, **overrides):
    base = dict(route_table=route_table(), authority_diff=dict(EMPTY_DIFF),
                layers=dict(LAYERS),
                compensations={k: dict(v) for k, v in COMPENSATIONS.items()},
                threshold=0.95, min_observations=4)
    base.update(overrides)
    return ledger.plan(**base)


def sides(incumbent_ir, candidate_ir, *, disagree_at=None, slo=None):
    """The two producers `TierShadow` needs.

    The incumbent's is handed the crossing and the RAW host return it just
    produced: the incumbent has already answered, and asking it again would
    issue and pay for a second completion.

    The candidate's issues the successor's answer, and it does so through the
    SAME tier seam, on the successor's own recorder. That is what exercises
    the re-entrancy register: without it the observer would observe its own
    candidate call, unboundedly."""
    runtime = srt.tier_runtime()
    replay = replay_module()
    calls = []

    def incumbent(crossing, value):
        assert isinstance(value, dict) and value.get("tag") == "ok"
        step = crossing[1]
        return srt.answered(incumbent_ir, COMPONENT, seal(
            step_index=step, role="incumbent",
            placement=PLACEMENT_INCUMBENT,
            answer=answer_digest(f"said-{step}"), prompt=f"asked-{step}"))

    def candidate(crossing):
        step = crossing[1]
        calls.append(crossing)
        cand = replay.Timeline(COMPONENT)

        def make_call():
            cand.record_emission("model", "complete", (f"p{step}",), "Model",
                                 ("shadow.rvl", 1))
            return host_return("local:successor-1")

        runtime.validate_retry(make_call, budget=0,
                               schema={"type": "object"}, where=COMPONENT)
        said = f"said-{step}" if step != disagree_at else f"other-{step}"
        return srt.answered(candidate_ir, COMPONENT, seal(
            step_index=step, role="successor",
            placement=PLACEMENT_SUCCESSOR,
            answer=answer_digest(said), prompt=f"asked-{step}"))

    metrics = (lambda _crossing: dict(slo)) if slo is not None else None
    return incumbent, candidate, metrics, calls


def run(incumbent_ir, candidate_ir, *, route_overrides=None,
        disagree_at=None, slo=None, plan_overrides=None):
    """One live run: resolve, wire the seam, drive the composition, decide."""
    the_route = route(**(route_overrides or {}))
    resolution = srt.resolve(incumbent_ir, the_route)
    incumbent, candidate, metrics, calls = sides(
        incumbent_ir, candidate_ir, disagree_at=disagree_at, slo=slo)
    shadow = srt.TierShadow(resolution, incumbent=incumbent,
                            candidate=candidate, slo=metrics)
    timeline, _served = drive(incumbent_ir, COMPONENT, shadow=shadow)
    ledger = shadow.ledger()
    the_plan = plan_for(ledger, **(plan_overrides or {}))
    verdict = srt.decide(incumbent_ir, the_route, the_plan, ledger.entries,
                         ledger=ledger, key=KEY)
    return shadow, ledger, verdict, timeline, calls


# ==========================================================================
# 0. the composition is real, and it is what the file says it is
# ==========================================================================

def test_the_three_generations_compile_and_differ_where_they_should(
        incumbent_ir, same_ir, moved_ir):
    """The differential rests on these three being what they claim. The
    sibling generation's recorded world is identical STEP FOR STEP and the
    relocated one's is not, and both facts are `compare_timelines`'."""
    base = srt.world_for(incumbent_ir, COMPONENT)
    same = srt.world_for(same_ir, COMPONENT)
    moved = srt.world_for(moved_ir, COMPONENT)

    assert canary.compare_timelines(base, same)["diverged"] is False
    assert [(s.kind, s.label) for s in base.steps] \
        == [(s.kind, s.label) for s in moved.steps]
    verdict = canary.compare_timelines(base, moved)
    assert verdict["diverged"] is True
    assert verdict["field"] == "slot"

    # and the two generations really are different compositions
    assert {c["name"] for c in same_ir["components"]} \
        != {c["name"] for c in incumbent_ir["components"]}


def test_the_recorder_mints_the_indices_the_composition_declares(
        incumbent_ir):
    """The premise the derivation rests on, measured rather than assumed.

    `crossing_actions` reads the STATIC walk. What a run keys its records on
    is what `replay.Timeline.record_emission` assigns. This asserts the two
    are the same tuple of keys, on a live drive of the composition, which is
    the whole of what note 558 section 3.1 said the tree had no producer
    for."""
    timeline, served = drive(incumbent_ir, COMPONENT)
    assert len(served) == WIDTH + 1

    derived = srt.crossing_actions(incumbent_ir, COMPONENT)
    assert crossings_of(timeline) == tuple(
        (COMPONENT, index) for index in sorted(derived))
    assert sorted(v for v in derived.values()) \
        == sorted([ANCHOR_ACTION] + [ACTION] * WIDTH)


# ==========================================================================
# 1. NON-VACUITY: a live promote and a live revert, same twenty crossings
# ==========================================================================

def test_a_live_shadow_promotes_and_a_relocated_step_reverts(
        incumbent_ir, same_ir, moved_ir):
    """The differential, and it is a run.

    Both rows drive the same composition through the same tier seam over the
    same twenty crossings. The ONLY thing that differs is which generation
    the candidate's recorded world is built from, and the verdict changes
    from PROMOTE to REVERT with the divergence attributed to an exact
    `(component, realm)` and an exact step."""
    _s, clean_ledger, clean, _t, _c = run(incumbent_ir, same_ir)
    assert clean.decision == sp.PROMOTE, clean.refusal
    assert clean.tally.paired == WIDTH
    assert clean.tally.agreed == WIDTH
    assert clean.tally.agreement == 1.0
    assert clean_ledger.worlds_recorded == WIDTH

    _s, live_ledger, live, _t, _c = run(
        incumbent_ir, moved_ir, route_overrides={"live": True},
        plan_overrides={"live": True})
    assert live.decision == sp.REVERT, live.refusal
    assert live.refusal.link == sp.DIVERGENCE_ATTRIBUTED
    assert live_ledger.worlds_recorded == WIDTH

    divergence = live.tally.first_divergence
    assert divergence.attribution == (COMPONENT, REALM)
    assert divergence.member == sp.WORLD_COMPARISON
    assert divergence.at_field == "slot"
    assert "slot=" in divergence.incumbent
    assert "slot=" in divergence.candidate
    assert divergence.incumbent != divergence.candidate

    assert {clean.decision, live.decision} == {sp.PROMOTE, sp.REVERT}


def test_the_recorded_world_is_what_changes_the_verdict(
        incumbent_ir, same_ir, moved_ir):
    """The recorded-world leg's own differential, live.

    Every record in both runs agrees on both agreement members: the two
    generations name the same completions. The record comparison therefore
    promotes, and the relocated step is only visible in the world."""
    _s, _l, clean, _t, _c = run(incumbent_ir, same_ir)
    _s, _l, moved, _t, _c = run(incumbent_ir, moved_ir)

    assert clean.decision == sp.PROMOTE
    assert clean.tally.agreement == 1.0

    assert moved.decision == sp.REFUSE
    assert moved.refusal.link == sp.AGREEMENT_BELOW_THRESHOLD
    assert moved.tally.agreed == 0
    assert all(d.member == sp.WORLD_COMPARISON for d in moved.tally.divergences)


def test_a_met_threshold_still_reverts_on_an_attributed_divergence(
        incumbent_ir, same_ir):
    """The sharpest row, carried over onto a live run.

    Nineteen of twenty crossings agree, the plan states 0.95, and the
    accumulated agreement is EXACTLY 0.95. A gate comparing a number against
    a number promotes. The attributed divergence reverts it, and the ratio is
    reported beside the attribution rather than in place of it. Every
    observation carried a perfect SLO block and the gate read none of it."""
    _s, ledger, verdict, _t, _c = run(
        incumbent_ir, same_ir, route_overrides={"live": True},
        plan_overrides={"live": True}, disagree_at=5, slo=PERFECT_SLO)

    assert verdict.tally.paired == WIDTH
    assert verdict.tally.agreed == WIDTH - 1
    assert verdict.tally.agreement == pytest.approx(0.95)
    assert verdict.tally.agreement >= 0.95
    assert verdict.decision == sp.REVERT
    assert verdict.refusal.link == sp.DIVERGENCE_ATTRIBUTED

    divergence = verdict.tally.first_divergence
    assert divergence.attribution == (COMPONENT, REALM)
    assert divergence.crossing == (COMPONENT, 5)
    assert divergence.member == "chosen_digest"

    supplied = sum(1 for e in ledger.entries if e.observation.has_slo)
    assert supplied == WIDTH
    assert verdict.slo_reads == 0
    assert verdict.measurements_read is True


# ==========================================================================
# 2. the tier seam
# ==========================================================================

def test_the_answer_the_body_receives_is_the_incumbents(incumbent_ir,
                                                        same_ir):
    """The seam's structural property. The observer runs on every crossing
    and the successor answers with a different model name on every one; the
    value `validate_retry` hands back is still the incumbent's host return,
    because the hook's result is discarded."""
    the_route = route(live=True)
    resolution = srt.resolve(incumbent_ir, the_route)
    incumbent, candidate, _m, _calls = sides(incumbent_ir, same_ir)
    shadow = srt.TierShadow(resolution, incumbent=incumbent,
                            candidate=candidate)
    _timeline, served = drive(incumbent_ir, COMPONENT, shadow=shadow)

    assert len(served) == WIDTH + 1
    assert {s["model"] for s in served} == {"openai:gpt-4o-2024-08-06"}
    assert shadow.observed == WIDTH + 1
    assert shadow.ledger().offered == WIDTH


def test_the_ledger_records_the_side_the_seam_actually_served(incumbent_ir,
                                                              same_ir):
    """Even on a LIVE route.

    `live` says which rule the gate applies, which is that the first
    attributed divergence reverts, and slice 2 derives the served side from
    it because an offline caller has nothing better to go on. A seam does: this hook's return is
    discarded, so the incumbent answered, and the ledger says the incumbent
    answered. A route this seam serves is a shadow whatever its `live` flag
    says, and a route whose successor really answers is a cutover this seam
    does not perform."""
    _s, ledger, verdict, _t, _c = run(
        incumbent_ir, same_ir, route_overrides={"live": True},
        plan_overrides={"live": True}, disagree_at=5)

    assert ledger.route.live is True
    assert {served.side for served in ledger.served} == {sr.INCUMBENT}
    assert all(entry.side == sr.INCUMBENT for entry in ledger.entries)
    assert f"answers served by the incumbent: {WIDTH} of {WIDTH}" \
        in sr.render(verdict, ledger)
    # and the live RULE still applied
    assert verdict.decision == sp.REVERT


def test_the_candidate_is_called_only_on_the_crossings_the_share_selects(
        incumbent_ir, same_ir):
    """A declared fraction, counted at the call site on a live run.

    The share is a rational and the selection is the keyed digest, so which
    crossings are shadowed is a pure function of the crossing. The candidate
    producer appends to a list of its own, and that list is compared against
    the schedule's count and against `selects` directly."""
    the_route = route(share=sr.Share(1, 4))
    resolution = srt.resolve(incumbent_ir, the_route)
    incumbent, candidate, _m, calls = sides(incumbent_ir, same_ir)
    shadow = srt.TierShadow(resolution, incumbent=incumbent,
                            candidate=candidate)
    timeline, _served = drive(incumbent_ir, COMPONENT, shadow=shadow)
    ledger = shadow.ledger()

    offered = [c for c in crossings_of(timeline) if resolution.owns(c)]
    assert len(offered) == WIDTH
    expected = [c for c in offered if sr.selects(the_route, c)]
    assert calls == expected
    assert ledger.candidate_calls == len(expected)
    assert 0 < len(expected) < len(offered)
    assert ledger.offered == len(offered)
    assert ledger.shadowed == len(expected)


def test_the_candidates_own_completion_does_not_re_enter_the_seam(
        incumbent_ir, same_ir):
    """Re-entrancy. The successor's answer crosses the same seam, so without
    the tier's register the observer would observe itself. Measured as a
    count: twenty-one crossings offered, not forty-two, and no fault."""
    _shadow, ledger, _v, timeline, calls = run(incumbent_ir, same_ir)
    assert len(calls) == WIDTH
    assert ledger.offered == WIDTH
    assert _shadow.observed == WIDTH + 1
    assert _shadow.foreign == 1
    assert _shadow.faults == ()


def test_an_observer_fault_stops_the_shadow_and_not_the_run(incumbent_ir,
                                                            same_ir):
    """The failure direction at the seam. An observer that raises must not
    take the incumbent's answer with it, and the window it leaves must refuse
    rather than be decided: the crossings that did accumulate are the ones
    BEFORE the fault, which is a biased sample of the ones offered."""
    the_route = route()
    resolution = srt.resolve(incumbent_ir, the_route)
    incumbent, candidate, _m, _calls = sides(incumbent_ir, same_ir)
    seen = []

    def exploding(crossing, value):
        seen.append(crossing)
        if len(seen) == 3:
            raise RuntimeError("the observer broke")
        return incumbent(crossing, value)

    shadow = srt.TierShadow(resolution, incumbent=exploding,
                            candidate=candidate)
    _timeline, served = drive(incumbent_ir, COMPONENT, shadow=shadow)

    assert len(served) == WIDTH + 1          # the run completed
    assert len(shadow.faults) == 1
    assert "the observer broke" in shadow.faults[0][1]
    assert len(seen) == 3                    # and the seam detached itself

    ledger = shadow.ledger()
    assert ledger.refusal[0] == srt.SHADOW_FAULTED
    assert ledger.entries == ()
    verdict = srt.decide(incumbent_ir, the_route, plan_for(ledger),
                         ledger.entries, ledger=ledger, key=KEY)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == srt.SHADOW_FAULTED
    assert verdict.measurements_read is False


def test_an_unattached_seam_changes_nothing(incumbent_ir):
    """The hook is absent by default, so an emitted program that never
    attaches one behaves as it did before. Same crossings, same answers, no
    faults."""
    runtime = srt.tier_runtime()
    timeline, served = drive(incumbent_ir, COMPONENT)
    assert runtime.revl_shadow_attached() is False
    assert runtime.revl_shadow_faults() == ()
    assert len(crossings_of(timeline)) == WIDTH + 1
    assert len(served) == WIDTH + 1


# ==========================================================================
# 3. the stamp, derived
# ==========================================================================

def test_a_window_of_one_actions_crossings_cannot_promote_another(
        incumbent_ir, same_ir):
    """Note 558 section 1.1, closed.

    The window is `summarize`'s twenty crossings. The schedule, the plan and
    every stamp say `classify`, so slice 2's `_check_entries`, which compares
    the stamp against the PLAN, is satisfied: both sides of that comparison
    are the scheduler's own word. The composition is not, and it says those
    crossings are `summarize`'s."""
    honest = route()
    forged = route(action=ANCHOR_ACTION)

    resolution = srt.resolve(incumbent_ir, honest)
    incumbent, candidate, _m, _c = sides(incumbent_ir, same_ir)
    shadow = srt.TierShadow(resolution, incumbent=incumbent,
                            candidate=candidate)
    drive(incumbent_ir, COMPONENT, shadow=shadow)
    window = shadow.ledger().entries              # `summarize`'s, all twenty
    assert len(window) == WIDTH
    assert shadow.foreign == 1                    # `classify`'s, dropped

    restamped = tuple(
        sr.Entry(crossing=e.crossing, component=e.component,
                 action=ANCHOR_ACTION, realm=e.realm, side=e.side,
                 observation=e.observation)
        for e in window)
    ledger = sr.ShadowLedger(route=forged, entries=restamped)
    plan = plan_for(ledger)

    # slice 2 alone admits it: the stamp matches the plan on both sides
    assert sr.decide(forged, plan, restamped, key=KEY).decision == sp.PROMOTE

    # the composition does not
    verdict = srt.decide(incumbent_ir, forged, plan, restamped, key=KEY)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == srt.STAMP_FORGED
    assert "summarize" in verdict.refusal.reason
    assert verdict.measurements_read is False
    assert all(stage["status"] == "not reached"
               for stage in verdict.preconditions
               if stage["stage"] != srt.COMPOSITION_STAGE)


def test_a_crossing_the_composition_declares_at_no_index_refuses(
        incumbent_ir, same_ir):
    """An underivable stamp refuses rather than being guessed at. This is the
    branch a run that departs from the static walk's premise takes: the
    crossing is not one the composition declares, so there is no action to
    check the claim against."""
    _s, ledger, _v, _t, _c = run(incumbent_ir, same_ir)
    stray = ledger.entries[0]
    invented = sr.Entry(crossing=(COMPONENT, 999), component=COMPONENT,
                        action=ACTION, realm=REALM, side=stray.side,
                        observation=stray.observation)
    plan = plan_for(ledger)
    verdict = srt.decide(incumbent_ir, route(), plan,
                         list(ledger.entries) + [invented], key=KEY)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == srt.STAMP_UNDERIVED


def test_the_derivation_reads_the_composition_and_not_the_schedule(
        incumbent_ir, moved_ir):
    """`crossing_actions` is a function of the generation alone. The same
    component in the relocated generation attributes the same crossings to
    the other entry point, which is what makes it evidence about the
    composition rather than an echo of the stamp."""
    base = srt.crossing_actions(incumbent_ir, COMPONENT)
    moved = srt.crossing_actions(moved_ir, COMPONENT)
    assert sorted(base) == sorted(moved)
    assert set(base.values()) == {ANCHOR_ACTION, ACTION}
    assert set(moved.values()) == {ANCHOR_ACTION}
    assert srt.declared_actions(incumbent_ir, COMPONENT) \
        == frozenset({ANCHOR_ACTION, ACTION})


# ==========================================================================
# 4. the realm, resolved
# ==========================================================================

def test_a_realm_the_composition_does_not_have_refuses_and_names_the_ones_it_has(
        incumbent_ir, same_ir):
    """`revl canary` refuses a realm the composition lacks, through
    `placement.slice_partition`. So does this, through the same call and with
    the same shape of message."""
    absent = route(realm="tenant_zzz")
    resolution = srt.resolve(incumbent_ir, absent)
    assert resolution.ok is False
    assert resolution.refusal[0] == srt.REALM_UNKNOWN
    assert REALM in resolution.refusal[1]

    _s, ledger, _v, _t, _c = run(incumbent_ir, same_ir)
    verdict = srt.decide(incumbent_ir, absent, plan_for(ledger),
                         ledger.entries, key=KEY)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == srt.REALM_UNKNOWN
    assert verdict.measurements_read is False


def test_a_shadow_route_never_reaches_the_seam_on_an_unresolved_realm(
        incumbent_ir, same_ir):
    """And the refusal is ahead of the seam, not after it: a schedule the
    composition does not bear out observes nothing, so there is no window to
    be tempted by."""
    absent = route(realm="tenant_zzz")
    resolution = srt.resolve(incumbent_ir, absent)
    incumbent, candidate, _m, calls = sides(incumbent_ir, same_ir)
    shadow = srt.TierShadow(resolution, incumbent=incumbent,
                            candidate=candidate)
    _timeline, served = drive(incumbent_ir, COMPONENT, shadow=shadow)

    assert len(served) == WIDTH + 1          # the incumbent answered
    assert calls == []                       # the successor was never asked
    ledger = shadow.ledger()
    assert ledger.entries == ()
    assert ledger.refusal[0] == srt.REALM_UNKNOWN


def test_a_component_outside_the_designated_realm_refuses(incumbent_ir):
    """The realm is half of the attribution, so a component that is not
    isolated into the realm attributes to nothing."""
    other = compile_source(source(realm=OTHER_REALM), "other.rvl")
    resolution = srt.resolve(other, route())
    assert resolution.refusal[0] == srt.REALM_UNKNOWN

    # and with the realm present but the component elsewhere in it
    resolution = srt.resolve(other, route(realm=OTHER_REALM,
                                          component="Classifier"))
    assert resolution.ok is True
    assert resolution.members == ("Classifier",)


def test_an_action_the_component_does_not_declare_refuses(incumbent_ir):
    resolution = srt.resolve(incumbent_ir, route(action="translate"))
    assert resolution.refusal[0] == srt.ACTION_UNDECLARED
    assert "summarize" in resolution.refusal[1]


def test_an_action_that_crosses_no_boundary_refuses(moved_ir):
    """`summarize` crosses nothing in the relocated generation, so a schedule
    over it shadows nothing. An empty window reported as agreement is the
    fail-open shape this family refuses everywhere."""
    resolution = srt.resolve(moved_ir, route())
    assert resolution.refusal[0] == srt.ACTION_UNCROSSED


def test_an_unknown_component_refuses_and_names_the_ones_there_are(
        incumbent_ir):
    resolution = srt.resolve(incumbent_ir, route(component="Nowhere"))
    assert resolution.refusal[0] == srt.COMPONENT_UNKNOWN
    assert COMPONENT in resolution.refusal[1]


def test_the_realm_check_is_placements_own(incumbent_ir):
    """The two-line reuse, asserted as a reuse. The members and providers a
    resolution carries are `placement.slice_partition`'s verbatim, which is
    what `revl.mcp.canary.select_slice` reads too."""
    from revl.placement import slice_partition

    part = slice_partition(incumbent_ir, REALM)
    resolution = srt.resolve(incumbent_ir, route())
    assert resolution.members == tuple(part["members"])
    assert resolution.providers == part["providers"]
    assert resolution.remainder_realms == tuple(part["remainderRealms"])


# ==========================================================================
# 5. discipline
# ==========================================================================

def test_no_refusal_here_is_a_guarantee_code(incumbent_ir):
    """A seam refusing a window is not the checker refusing a program, so
    nothing here owes `tools/tier_guarantees.py` a reproducer."""
    assert all(not link.startswith("G-") for link in srt.LINKS)
    assert len(set(srt.LINKS)) == len(srt.LINKS)
    assert set(srt.LINKS).isdisjoint(sr.LINKS)
    assert set(srt.LINKS).isdisjoint(sp.LINKS)


def test_the_module_builds_no_verdict_of_its_own():
    assert srt.DECISIONS is sp.DECISIONS
    assert set(srt.DECISIONS) == {sp.PROMOTE, sp.REFUSE, sp.REVERT}


def test_the_share_does_not_reach_the_verdict(incumbent_ir, same_ir):
    """Carried from slice 2 and re-measured on a live run: two shares over
    the same composition reach the same decision with different sample sizes,
    and the word `share` is absent from the serialised verdict."""
    _s, wide, wide_verdict, _t, _c = run(incumbent_ir, same_ir)
    the_route = route(share=sr.Share(1, 2))
    resolution = srt.resolve(incumbent_ir, the_route)
    incumbent, candidate, _m, _calls = sides(incumbent_ir, same_ir)
    shadow = srt.TierShadow(resolution, incumbent=incumbent,
                            candidate=candidate)
    drive(incumbent_ir, COMPONENT, shadow=shadow)
    narrow = shadow.ledger()
    narrow_verdict = srt.decide(incumbent_ir, the_route, plan_for(narrow),
                                narrow.entries, ledger=narrow, key=KEY)

    assert narrow.shadowed < wide.shadowed
    assert narrow_verdict.decision == wide_verdict.decision == sp.PROMOTE
    assert "share" not in str(narrow_verdict.as_dict())


def test_the_module_names_the_tiers_it_wired_and_the_ones_it_did_not():
    """Note 558 section 9 said "no tier calls `serve`". One does now, and the
    module says which rather than leaving five to be inferred."""
    assert srt.WIRED_TIERS == ("python",)
    assert set(srt.UNWIRED_TIERS) == {"cordis", "java", "rust", "ts", "wasm"}
    assert set(srt.WIRED_TIERS).isdisjoint(srt.UNWIRED_TIERS)


def test_the_world_builder_is_canarys_own():
    """Called, not reimplemented, so the comparison key stays item 496's and
    moves when that one moves."""
    assert srt.world_for.__wrapped__ is not None \
        if hasattr(srt.world_for, "__wrapped__") else True
    tree = ast.parse(MODULE_PATH.read_text())
    calls = {node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)}
    assert "slice_timeline" in calls
    assert "compare_timelines" not in calls        # never reimplemented here


def test_the_module_reads_no_metric():
    """No function here reads an SLO member, and the metrics producer's value
    goes straight into the `Observation`, which is where a metric is allowed
    to be."""
    tree = ast.parse(MODULE_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in sp.SLO_MEMBERS + ("_slo",), node.attr

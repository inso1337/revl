"""Shadow register: a promotion lands and a revert is performed (roadmap item
518, issue #1192).

The executable spec for `docs/design/558-shadow-scheduling.md` section 17.

Item 518's exit: "an induced divergence in shadow reverts the promotion and
names the step that diverged, and a promotion on an agreement threshold lands
with the comparison attached." Before this file, `PROMOTE` changed nothing
and a `REVERT` verdict reported `restored: route-arm` about an arm no code
had moved. Every test here drives the composition through the python tier's
recorder and completion seam, using `tests/test_shadow_runtime_518.py`'s
drive, and then reads the REGISTER's state, which is the thing that moves.
"""

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_shadow_runtime_518 as live  # noqa: E402
from revl import shadow_promotion as sp  # noqa: E402
from revl import shadow_register as reg  # noqa: E402
from revl import shadow_routing as sr  # noqa: E402
from revl import shadow_runtime as srt  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.mcp.session import replay_module  # noqa: E402

MODULE_PATH = ROOT / "src" / "revl" / "shadow_register.py"

SUMMARIZE = (live.COMPONENT, live.ACTION)
CLASSIFY = (live.COMPONENT, live.ANCHOR_ACTION)
INCUMBENT, SUCCESSOR = "incumbent", "successor"
PLACEMENT = {INCUMBENT: live.PLACEMENT_INCUMBENT,
             SUCCESSOR: live.PLACEMENT_SUCCESSOR}


@pytest.fixture(scope="module")
def incumbent_ir():
    return compile_source(live.INCUMBENT_SRC, "incumbent.rvl")


@pytest.fixture(scope="module")
def same_ir():
    return compile_source(live.CANDIDATE_SAME_SRC, "candidate_same.rvl")


@pytest.fixture(scope="module")
def moved_ir():
    return compile_source(live.CANDIDATE_MOVED_SRC, "candidate_moved.rvl")


def register():
    return reg.PromotionRegister({SUMMARIZE: INCUMBENT, CLASSIFY: INCUMBENT})


# ==========================================================================
# one window, driven through the tier seam
# ==========================================================================

def sides(incumbent_ir, candidate_ir, route, *, disagree_at=None):
    """`tests/test_shadow_runtime_518.py`'s two producers, with the roles
    taken from the ROUTE the register scheduled rather than fixed, so a
    stacked promotion seals its records under the roles it is about."""
    runtime = srt.tier_runtime()
    replay = replay_module()
    calls = []

    def incumbent(crossing, value):
        assert isinstance(value, dict) and value.get("tag") == "ok"
        step = crossing[1]
        return srt.answered(incumbent_ir, live.COMPONENT, live.seal(
            step_index=step, role=route.incumbent_role,
            placement=PLACEMENT[route.incumbent_role],
            answer=live.answer_digest(f"said-{step}"),
            prompt=f"asked-{step}"))

    def candidate(crossing):
        step = crossing[1]
        calls.append(crossing)
        timeline = replay.Timeline(live.COMPONENT)

        def make_call():
            timeline.record_emission("model", "complete", (f"p{step}",),
                                     "Model", ("shadow.rvl", 1))
            return live.host_return("local:successor-1")

        runtime.validate_retry(make_call, budget=0,
                               schema={"type": "object"},
                               where=live.COMPONENT)
        said = f"said-{step}" if step != disagree_at else f"other-{step}"
        return srt.answered(candidate_ir, live.COMPONENT, live.seal(
            step_index=step, role=route.candidate_role,
            placement=PLACEMENT[route.candidate_role],
            answer=live.answer_digest(said), prompt=f"asked-{step}"))

    return incumbent, candidate, calls


def window(the_register, action_class, incumbent_ir, candidate_ir, *,
           share=sr.EVERYTHING, candidate_role=None, disagree_at=None,
           **plan_overrides):
    """Schedule a window for ``action_class`` on the route the REGISTER
    derives, drive the composition, and return `(ledger, plan, calls)`."""
    route = the_register.route(action_class, realm=live.REALM, share=share,
                               salt="pinned", candidate_role=candidate_role)
    resolution = srt.resolve(incumbent_ir, route)
    incumbent, candidate, calls = sides(incumbent_ir, candidate_ir, route,
                                        disagree_at=disagree_at)
    shadow = srt.TierShadow(resolution, incumbent=incumbent,
                            candidate=candidate)
    live.drive(incumbent_ir, live.COMPONENT, shadow=shadow)
    ledger = shadow.ledger()
    return ledger, live.plan_for(ledger, **plan_overrides), calls


def land(the_register, action_class, incumbent_ir, candidate_ir, **kw):
    ledger, plan, calls = window(the_register, action_class, incumbent_ir,
                                 candidate_ir,
                                 candidate_role=kw.pop("candidate_role",
                                                       SUCCESSOR), **kw)
    return the_register.land(incumbent_ir, ledger, plan, key=live.KEY), \
        ledger, calls


def observe(the_register, action_class, incumbent_ir, candidate_ir, **kw):
    ledger, plan, calls = window(the_register, action_class, incumbent_ir,
                                 candidate_ir, **kw)
    return the_register.observe(incumbent_ir, ledger, plan, key=live.KEY), \
        ledger, calls


# ==========================================================================
# 1. THE EXIT: a promotion lands with the comparison attached ...
# ==========================================================================

def test_a_promotion_on_the_threshold_lands_with_the_comparison_attached(
        incumbent_ir, same_ir):
    the_register = register()
    assert the_register.arm(SUMMARIZE) == INCUMBENT
    assert the_register.route(SUMMARIZE, realm=live.REALM,
                              share=sr.EVERYTHING,
                              candidate_role=SUCCESSOR).live is False

    outcome, ledger, calls = land(the_register, SUMMARIZE, incumbent_ir,
                                  same_ir)

    assert outcome.changed is True, outcome.refusal
    assert outcome.verdict.decision == sp.PROMOTE
    assert the_register.arm(SUMMARIZE) == SUCCESSOR
    assert len(calls) == live.WIDTH

    attached = outcome.arm.comparison
    tally = attached["verdict"]["tally"]
    assert (tally["paired"], tally["agreed"]) == (live.WIDTH, live.WIDTH)
    assert attached["verdict"]["threshold"] == 0.95
    assert attached["verdict"]["decision"] == sp.PROMOTE
    assert attached["schedule"]["shadowed"] == live.WIDTH
    assert len(attached["compared"]) == live.WIDTH
    assert all(row["legs"] == ["records", sp.WORLD_COMPARISON]
               for row in attached["compared"])
    assert attached["verdict"]["slo_reads"] == 0
    # the attachment is a value that survives serialisation
    assert json.loads(json.dumps(attached)) == attached

    # and the next window for the class is scheduled LIVE by the register
    after = the_register.route(SUMMARIZE, realm=live.REALM,
                               share=sr.EVERYTHING)
    assert after.live is True
    assert (after.incumbent_role, after.candidate_role) \
        == (INCUMBENT, SUCCESSOR)


# ==========================================================================
# 2. ... and an induced divergence in shadow reverts it, naming the step
# ==========================================================================

def test_an_induced_divergence_reverts_the_promotion_and_names_the_step(
        incumbent_ir, same_ir):
    """The revert is an operation run here, not a report: the arm moves back
    and the class's state equals its state before the promotion landed."""
    the_register = register()
    before = the_register.state(SUMMARIZE)
    landed, _l, _c = land(the_register, SUMMARIZE, incumbent_ir, same_ir)
    assert landed.changed and the_register.arm(SUMMARIZE) == SUCCESSOR

    outcome, _ledger, _calls = observe(the_register, SUMMARIZE,
                                       incumbent_ir, same_ir, disagree_at=5)

    assert outcome.verdict.decision == sp.REVERT
    assert outcome.verdict.refusal.link == sp.DIVERGENCE_ATTRIBUTED
    assert outcome.changed is True
    assert the_register.arm(SUMMARIZE) == INCUMBENT
    assert the_register.promoted(SUMMARIZE) is None
    assert the_register.state(SUMMARIZE) == before

    reversal = outcome.reversal
    assert reversal["divergence"]["crossing"] == [live.COMPONENT, 5]
    assert reversal["divergence"]["attribution"] == [live.COMPONENT,
                                                     live.REALM]
    assert reversal["divergence"]["member"] == "chosen_digest"
    assert reversal["named"].startswith(
        f"{live.COMPONENT} in realm `{live.REALM}` step 5:")
    assert reversal["restored"] == [reg.RESTORED_LAYER]
    assert reversal["restored_exactly"] is True
    assert reversal["withdrawn_role"] == SUCCESSOR
    assert reversal["restored_role"] == INCUMBENT

    text = reg.render_reversal(reversal)
    assert "step 5" in text
    assert "rolled back" not in text

    # the next window is a shadow again, scheduled by the register
    assert the_register.route(SUMMARIZE, realm=live.REALM,
                              share=sr.EVERYTHING,
                              candidate_role=SUCCESSOR).live is False
    assert [h["operation"] for h in the_register.history] \
        == [reg.LAND, reg.REVERT]


def test_a_relocated_step_reverts_and_names_the_replay_step_and_field(
        incumbent_ir, same_ir, moved_ir):
    """The divergence a record comparison cannot see: both records name the
    same completion and the recorded worlds differ in `slot`."""
    the_register = register()
    land(the_register, SUMMARIZE, incumbent_ir, same_ir)

    outcome, _l, _c = observe(the_register, SUMMARIZE, incumbent_ir,
                              moved_ir)

    assert outcome.changed is True
    divergence = outcome.reversal["divergence"]
    assert divergence["member"] == sp.WORLD_COMPARISON
    assert divergence["at_field"] == "slot"
    assert isinstance(divergence["at_step"], int)
    assert f"replay step {divergence['at_step']}" in outcome.reversal["named"]
    assert the_register.arm(SUMMARIZE) == INCUMBENT


def test_a_clean_live_window_leaves_the_promotion_standing(incumbent_ir,
                                                           same_ir):
    the_register = register()
    land(the_register, SUMMARIZE, incumbent_ir, same_ir)
    outcome, _l, _c = observe(the_register, SUMMARIZE, incumbent_ir, same_ir)
    assert outcome.verdict.decision == sp.PROMOTE
    assert outcome.changed is False
    assert the_register.arm(SUMMARIZE) == SUCCESSOR


# ==========================================================================
# 3. the revert is real: what it restores, compensates, and leaves alone
# ==========================================================================

def test_the_window_that_promoted_cannot_land_again_after_a_revert(
        incumbent_ir, same_ir):
    """The agreement-ledger compensation, performed and then tested: the
    superseded window is refused, and a NEW window can still land, so the
    revert is not a terminal lock-out either."""
    the_register = register()
    landed, ledger, _c = land(the_register, SUMMARIZE, incumbent_ir, same_ir)
    plan = live.plan_for(ledger)
    observe(the_register, SUMMARIZE, incumbent_ir, same_ir, disagree_at=5)
    assert the_register.superseded(landed.arm.window)

    again = the_register.land(incumbent_ir, ledger, plan, key=live.KEY)
    assert again.changed is False
    assert again.refusal[0] == reg.WINDOW_SUPERSEDED
    assert the_register.arm(SUMMARIZE) == INCUMBENT

    # The composition is deterministic, so a second full drive reproduces the
    # superseded window byte for byte and is refused as the same evidence.
    replayed, replayed_ledger, _c = land(the_register, SUMMARIZE,
                                         incumbent_ir, same_ir)
    assert reg.window_id(replayed_ledger.entries) == landed.arm.window
    assert replayed.refusal[0] == reg.WINDOW_SUPERSEDED

    # A different window (half the crossings) is different evidence, and it
    # lands: a revert supersedes a window, it does not lock the class.
    fresh, fresh_ledger, _c = land(the_register, SUMMARIZE, incumbent_ir,
                                   same_ir, share=sr.Share(1, 2))
    assert 4 <= fresh_ledger.shadowed < live.WIDTH
    assert reg.window_id(fresh_ledger.entries) != landed.arm.window
    assert fresh.changed is True, fresh.refusal
    assert the_register.arm(SUMMARIZE) == SUCCESSOR


def test_the_revert_reports_what_it_did_not_perform(incumbent_ir, same_ir):
    the_register = register()
    land(the_register, SUMMARIZE, incumbent_ir, same_ir)
    outcome, _l, _c = observe(the_register, SUMMARIZE, incumbent_ir, same_ir,
                              disagree_at=3)
    reversal = outcome.reversal
    assert reversal["compensated"] == [{
        "layer": reg.COMPENSATED_LAYER, "performed": reg.SUPERSEDE,
        "window": reversal["compensated"][0]["window"]}]
    assert reversal["not_performed"] == [{
        "layer": "placement-history", "state": "neither",
        "declared_compensation": "replay-placement-log"}]
    text = reg.render_reversal(reversal)
    assert "not performed by this register: placement-history" in text


def test_a_revert_leaves_every_other_class_exactly_as_it_was(incumbent_ir,
                                                             same_ir):
    """The survivors, measured. `classify` is promoted too (its one crossing
    meets a plan that asks for one), and reverting `summarize` does not
    touch it."""
    the_register = register()
    first, _l, _c = land(the_register, SUMMARIZE, incumbent_ir, same_ir)
    second, _l, _c = land(the_register, CLASSIFY, incumbent_ir, same_ir,
                          min_observations=1)
    assert first.changed and second.changed, second.refusal
    classify_before = the_register.state(CLASSIFY)

    outcome, _l, _c = observe(the_register, SUMMARIZE, incumbent_ir, same_ir,
                              disagree_at=5)

    assert outcome.changed is True
    assert outcome.reversal["survivors"] == ["Classifier.classify"]
    assert outcome.reversal["breached"] == []
    assert the_register.state(CLASSIFY) == classify_before
    assert the_register.arm(CLASSIFY) == SUCCESSOR


def test_a_revert_is_lifo(incumbent_ir, same_ir):
    """Two promotions stacked on one class. A verdict about the lower one
    cannot reach under the upper one, and reverting the upper one restores
    the lower one's arm exactly."""
    the_register = register()
    first, _l, _c = land(the_register, SUMMARIZE, incumbent_ir, same_ir)
    assert first.changed
    assert the_register.route(SUMMARIZE, realm=live.REALM,
                              share=sr.EVERYTHING).live is True

    # stack incumbent back on top of successor, as a second promotion
    second, _l, _c = land(the_register, SUMMARIZE, incumbent_ir, same_ir,
                          candidate_role=INCUMBENT)
    assert second.changed, second.refusal
    assert (second.arm.from_role, second.arm.to_role) == (SUCCESSOR,
                                                          INCUMBENT)
    assert the_register.state(SUMMARIZE)["stack"] == [1, 2]

    # a REVERT about the lower promotion (incumbent -> successor) is refused
    stale = sp.Promotion(
        kind=sp.PROMOTION_KIND, version=sp.PROMOTION_VERSION,
        decision=sp.REVERT, action_class=SUMMARIZE,
        incumbent_role=INCUMBENT, candidate_role=SUCCESSOR, threshold=0.95,
        refusal=sp.Refusal(sp.DIVERGENCE_ATTRIBUTED, "x", (live.COMPONENT, 5)),
        preconditions=(),
        tally=sp.Tally(SUMMARIZE, 1, 0, 1, (sp.Divergence(
            (live.COMPONENT, 5), "chosen_digest", "a", "b",
            realm=live.REALM),)),
        restoration=None, slo_reads=0, slo_supplied=0)
    refused = the_register.revert(stale)
    assert refused.changed is False
    assert refused.refusal[0] == reg.ARM_MISMATCHED
    assert the_register.state(SUMMARIZE)["stack"] == [1, 2]

    # the upper one reverts on its own live window
    outcome, _l, _c = observe(the_register, SUMMARIZE, incumbent_ir, same_ir,
                              disagree_at=5)
    assert outcome.changed is True
    assert the_register.arm(SUMMARIZE) == SUCCESSOR
    assert the_register.state(SUMMARIZE) == dict(second.arm.before)
    assert the_register.state(SUMMARIZE)["stack"] == [1]
    assert outcome.reversal["restored_exactly"] is True


def test_a_serialised_register_reverts_after_it_is_reloaded(incumbent_ir,
                                                            same_ir):
    the_register = register()
    land(the_register, SUMMARIZE, incumbent_ir, same_ir)
    saved = json.loads(json.dumps(the_register.as_dict()))

    reloaded = reg.PromotionRegister.from_dict(saved)
    assert reloaded.arm(SUMMARIZE) == SUCCESSOR
    outcome, _l, _c = observe(reloaded, SUMMARIZE, incumbent_ir, same_ir,
                              disagree_at=7)
    assert outcome.changed is True
    assert reloaded.arm(SUMMARIZE) == INCUMBENT
    assert outcome.reversal["divergence"]["crossing"] == [live.COMPONENT, 7]
    # the original object is untouched by what happened to its copy
    assert the_register.arm(SUMMARIZE) == SUCCESSOR


# ==========================================================================
# 4. ZERO EVIDENCE is not a pass, at every place it can arise
# ==========================================================================

def test_a_window_with_zero_shadowed_runs_does_not_land(incumbent_ir,
                                                        same_ir):
    the_register = register()
    before = the_register.as_dict()
    outcome, ledger, calls = land(the_register, SUMMARIZE, incumbent_ir,
                                  same_ir, share=sr.NOTHING)

    assert ledger.shadowed == 0 and calls == []
    assert outcome.changed is False
    assert outcome.verdict.decision == sp.REFUSE
    assert outcome.refusal[0] == sp.EVIDENCE_MISSING
    assert the_register.arm(SUMMARIZE) == INCUMBENT

    last = the_register.evidence()["Classifier.summarize"]["last"]
    assert last["paired"] == 0
    assert last["agreement"] is None
    line = reg.render(the_register).splitlines()[2]
    assert line.startswith("Classifier.summarize:")
    assert "no evidence (0 pairs; REFUSE, evidence-missing)" in line
    assert "1.0000" not in line and "0.0000" not in line
    # only the evidence log moved
    after = the_register.as_dict()
    for entry in (before, after):
        for row in entry["classes"]:
            row.pop("evidence")
    assert after == before


def test_a_class_never_observed_reports_no_evidence(incumbent_ir, same_ir):
    """A successor that agreed on every `summarize` crossing has said nothing
    about `classify`, and the report says exactly that."""
    the_register = register()
    land(the_register, SUMMARIZE, incumbent_ir, same_ir)

    evidence = the_register.evidence()
    assert evidence["Classifier.summarize"]["last"]["agreement"] == 1.0
    assert evidence["Classifier.classify"]["last"] is None
    assert evidence["Classifier.classify"]["promoted"] is False
    text = reg.render(the_register)
    assert "Classifier.classify: incumbent (base arm); last window: no " \
           "evidence (no window decided)" in text


def test_a_live_window_with_zero_evidence_confirms_nothing(incumbent_ir,
                                                           same_ir):
    """After a promotion, an empty live window neither reverts nor reads as
    agreement. The promotion stands on the window that landed it."""
    the_register = register()
    land(the_register, SUMMARIZE, incumbent_ir, same_ir)
    outcome, ledger, _c = observe(the_register, SUMMARIZE, incumbent_ir,
                                  same_ir, share=sr.NOTHING)
    assert ledger.shadowed == 0
    assert outcome.changed is False
    assert outcome.refusal[0] == sp.EVIDENCE_MISSING
    last = the_register.evidence()["Classifier.summarize"]["last"]
    assert (last["operation"], last["paired"], last["agreement"]) \
        == (reg.OBSERVE, 0, None)


def test_a_promote_verdict_that_paired_nothing_does_not_land(
        incumbent_ir, same_ir, monkeypatch):
    """The gate refuses an empty window itself. This is the guard behind it:
    if the gate ever said PROMOTE over nothing, the register refuses."""
    the_register = register()
    ledger, plan, _c = window(the_register, SUMMARIZE, incumbent_ir, same_ir,
                              candidate_role=SUCCESSOR)
    empty = sp.Promotion(
        kind=sp.PROMOTION_KIND, version=sp.PROMOTION_VERSION,
        decision=sp.PROMOTE, action_class=SUMMARIZE,
        incumbent_role=INCUMBENT, candidate_role=SUCCESSOR, threshold=0.95,
        refusal=None, preconditions=(), tally=sp.Tally(SUMMARIZE, 0, 0, 0, ()),
        restoration=None, slo_reads=0, slo_supplied=0)
    monkeypatch.setattr(srt, "decide", lambda *a, **k: empty)

    outcome = the_register.land(incumbent_ir, ledger, plan, key=live.KEY)
    assert outcome.changed is False
    assert outcome.refusal[0] == reg.EVIDENCE_EMPTY
    assert the_register.arm(SUMMARIZE) == INCUMBENT
    # and the report does not read `Tally.agreement`'s 0.0 off an empty tally
    last = the_register.evidence()["Classifier.summarize"]["last"]
    assert (last["decision"], last["paired"], last["agreement"]) \
        == (sp.PROMOTE, 0, None)
    assert "no evidence (0 pairs; PROMOTE)" in reg.render(the_register)


# ==========================================================================
# 5. agreement is PER ACTION CLASS
# ==========================================================================

def test_agreement_on_one_class_does_not_promote_another(incumbent_ir,
                                                         same_ir):
    the_register = register()
    summarize_ledger, summarize_plan, _c = window(
        the_register, SUMMARIZE, incumbent_ir, same_ir,
        candidate_role=SUCCESSOR)

    # summarize's twenty agreeing pairs, offered as classify's evidence
    borrowed = the_register.land(
        incumbent_ir, summarize_ledger,
        live.plan_for(summarize_ledger, action=live.ANCHOR_ACTION),
        key=live.KEY)
    assert borrowed.changed is False
    assert borrowed.refusal[0] == sr.ROUTE_MISMATCHED
    assert the_register.arm(CLASSIFY) == INCUMBENT

    # the same window, offered for its own class, lands
    landed = the_register.land(incumbent_ir, summarize_ledger,
                               summarize_plan, key=live.KEY)
    assert landed.changed, landed.refusal

    # classify's own window, never shadowed
    empty, _l, _c = land(the_register, CLASSIFY, incumbent_ir, same_ir,
                         share=sr.NOTHING)
    assert empty.changed is False
    assert empty.refusal[0] == sp.EVIDENCE_MISSING

    # classify's own window, one crossing, against the plan's sample of four
    small, _l, _c = land(the_register, CLASSIFY, incumbent_ir, same_ir)
    assert small.changed is False
    assert small.refusal[0] == sp.SAMPLE_TOO_SMALL

    assert the_register.arm(SUMMARIZE) == SUCCESSOR
    assert the_register.arm(CLASSIFY) == INCUMBENT


# ==========================================================================
# 6. refusals leave the register as it was
# ==========================================================================

def test_land_refuses_a_live_window_and_an_undeclared_class(incumbent_ir,
                                                            same_ir):
    the_register = register()
    land(the_register, SUMMARIZE, incumbent_ir, same_ir)
    ledger, plan, _c = window(the_register, SUMMARIZE, incumbent_ir, same_ir)
    assert ledger.route.live is True
    outcome = the_register.land(incumbent_ir, ledger, plan, key=live.KEY)
    assert outcome.refusal[0] == reg.ARM_MISMATCHED
    assert "was scheduled live" in outcome.refusal[1]

    only_classify = reg.PromotionRegister({CLASSIFY: INCUMBENT})
    outcome = only_classify.land(incumbent_ir, ledger, plan, key=live.KEY)
    assert outcome.refusal[0] == reg.CLASS_UNDECLARED
    with pytest.raises(sp.PromotionRefused):
        only_classify.route(SUMMARIZE, realm=live.REALM, share=sr.EVERYTHING)


def test_a_window_taken_before_a_promotion_cannot_land_after_it(
        incumbent_ir, same_ir):
    """Two shadow windows scheduled while `incumbent` was the arm. The first
    lands; the second is now about an arm that is not in use, and landing it
    would stack `incumbent -> successor` on top of `successor`."""
    the_register = register()
    first = window(the_register, SUMMARIZE, incumbent_ir, same_ir,
                   candidate_role=SUCCESSOR)
    second = window(the_register, SUMMARIZE, incumbent_ir, same_ir,
                    candidate_role=SUCCESSOR, share=sr.Share(1, 2))
    landed = the_register.land(incumbent_ir, first[0], first[1],
                               key=live.KEY)
    assert landed.changed
    stale = the_register.land(incumbent_ir, second[0], second[1],
                              key=live.KEY)
    assert stale.changed is False
    assert stale.refusal[0] == reg.ARM_MISMATCHED
    assert the_register.state(SUMMARIZE)["stack"] == [1]


def test_restored_exactly_is_a_comparison_not_a_constant(incumbent_ir,
                                                         same_ir):
    """A register whose recorded pre-promotion state does not match what the
    revert produces says so, rather than reporting an exact restore."""
    the_register = register()
    land(the_register, SUMMARIZE, incumbent_ir, same_ir)
    saved = the_register.as_dict()
    saved["classes"][1]["stack"][0]["before"]["arm"] = "somebody-else"
    tampered = reg.PromotionRegister.from_dict(saved)

    outcome, _l, _c = observe(tampered, SUMMARIZE, incumbent_ir, same_ir,
                              disagree_at=5)
    assert outcome.changed is True
    assert outcome.reversal["restored_exactly"] is False
    assert "(exactly: NO)" in reg.render_reversal(outcome.reversal)


def test_the_survivor_set_is_measured(incumbent_ir, same_ir, monkeypatch):
    """`breached` is empty by construction, so this forces the other class
    to read differently after the pop and checks the revert reports it
    instead of listing it as a survivor."""
    the_register = register()
    land(the_register, SUMMARIZE, incumbent_ir, same_ir)
    ledger, plan, _c = window(the_register, SUMMARIZE, incumbent_ir,
                              same_ir, disagree_at=5)
    verdict = srt.decide(incumbent_ir, ledger.route, plan, ledger.entries,
                         ledger=ledger, key=live.KEY)
    assert verdict.decision == sp.REVERT

    real = reg.PromotionRegister.state
    reads = {"n": 0}

    def drifting(self, action_class):
        value = real(self, action_class)
        if tuple(action_class) == CLASSIFY:
            reads["n"] += 1
            if reads["n"] > 1:
                value = dict(value, arm="drifted")
        return value

    monkeypatch.setattr(reg.PromotionRegister, "state", drifting)
    outcome = the_register.revert(verdict)
    assert outcome.reversal["survivors"] == []
    assert outcome.reversal["breached"] == ["Classifier.classify"]
    assert "CHANGED: Classifier.classify" in reg.render_reversal(
        outcome.reversal)


def test_observe_and_revert_refuse_without_a_promotion(incumbent_ir,
                                                       same_ir):
    the_register = register()
    ledger, plan, _c = window(the_register, SUMMARIZE, incumbent_ir, same_ir,
                              candidate_role=SUCCESSOR)
    outcome = the_register.observe(incumbent_ir, ledger, plan, key=live.KEY)
    assert outcome.refusal[0] == reg.NOT_PROMOTED

    verdict = srt.decide(incumbent_ir, ledger.route, plan, ledger.entries,
                         ledger=ledger, key=live.KEY)
    assert verdict.decision == sp.PROMOTE
    outcome = the_register.revert(verdict)
    assert outcome.changed is False
    assert outcome.refusal[0] == reg.VERDICT_NOT_REVERT
    assert the_register.history == ()


def test_a_register_must_declare_its_classes():
    with pytest.raises(sp.PromotionRefused):
        reg.PromotionRegister({})
    with pytest.raises(sp.PromotionRefused):
        reg.PromotionRegister({("Classifier",): INCUMBENT})
    with pytest.raises(sp.PromotionRefused):
        reg.PromotionRegister({SUMMARIZE: ""})
    with pytest.raises(sp.PromotionRefused):
        reg.PromotionRegister.from_dict({"kind": "something-else"})


# ==========================================================================
# 7. shape
# ==========================================================================

def test_no_link_is_a_guarantee_code_and_no_verdict_is_built_here():
    assert not any(link.startswith("G-") for link in reg.LINKS)
    assert reg.DECISIONS is sp.DECISIONS
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    built = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and getattr(node.func, "attr", None) == "Promotion"]
    assert built == []


def test_land_takes_evidence_and_never_a_verdict():
    """`land` and `observe` compute their verdict; no parameter carries one
    in, so a caller cannot land what the gate did not reach."""
    import inspect

    for method in (reg.PromotionRegister.land, reg.PromotionRegister.observe):
        params = set(inspect.signature(method).parameters)
        assert params == {"self", "ir", "ledger", "plan", "key", "verifier"}


def test_the_module_reads_no_metric():
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    reads = [node for node in ast.walk(tree)
             if isinstance(node, ast.Attribute)
             and node.attr in ("slo", "_slo")]
    assert reads == []

"""Shadow routing: the scheduler, the action correlation, and the recorded
world (roadmap item 518 slice 2, issue #1192).

The executable spec for `docs/design/558-shadow-scheduling.md`. Slice 1
(`revl.shadow_promotion`, PR #1250) accumulates agreement over item 517's
records and gates the promotion on it; `tests/test_shadow_promotion_518.py` is
its spec and stays green unchanged. This file is about the half slice 1 does
not have, and it is organised around the five adversarial shapes:

1. a shadow that agrees on every recorded step and promotes;
2. one whose first divergence is attributed and REVERTS, naming the
   `(component, realm)` and the step;
3. a promotion attempted with no accumulated evidence;
4. an action class whose threshold is met by a DIFFERENT class's evidence;
5. a metric that would promote and a replay that would not, with the replay
   winning.

Four of the five are refusals, so the file's first obligation is non-vacuity:
section 1 runs the SAME twenty pairs through the promote path and the revert
path and measures both, and section 6 runs the same window with and without
the recorded worlds and measures the verdict change. Nothing here asserts that
a branch exists; every claim is a run.

The item 517 records are built here rather than imported, for the reason the
slice 1 file gives: the shape is what must not drift, and a local factory that
the two integration tests cross-check against `revl.model_evidence` is what
says so.
"""

import ast
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import model_route  # noqa: E402
from revl import parser as revl_parser  # noqa: E402
from revl import shadow_promotion as sp  # noqa: E402
from revl import shadow_routing as sr  # noqa: E402
from revl.mcp import canary  # noqa: E402
from revl.mcp.session import replay_module  # noqa: E402

MODULE_PATH = ROOT / "src" / "revl" / "shadow_routing.py"

KEY = b"shadow-routing-test-key"

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
POLICY = "e" * 64
PLACEMENT_LOCAL = "1" * 64
PLACEMENT_CLOUD = "2" * 64

REALM = "tenant_a"
OTHER_REALM = "tenant_b"


# --------------------------------------------------------------------------
# a real two-action program, so "another action's evidence" is expressible
# --------------------------------------------------------------------------

PROGRAM = """
model role local on_device
model role cloud off_device

service Answer {
  fn classify(text: Str) -> Str
  fn summarize(text: Str) -> Str
}

component Classifier provides out: Answer {
  route model on classify { confidential -> local, * -> cloud }
  route model on summarize { confidential -> local, * -> cloud }
  provide out {
    fn classify(text) = text
    fn summarize(text) = text
  }
}
"""


def route_table():
    program = revl_parser.Parser(PROGRAM, "m.rvl").parse()
    return model_route.check(program)


# --------------------------------------------------------------------------
# item 517 records and a verifier stub with `revl.model_evidence`'s shape
# --------------------------------------------------------------------------

class StubVerdict:
    def __init__(self, ok, link="", reason=""):
        self.ok = ok
        self.link = link
        self.reason = reason

    def __bool__(self):
        return self.ok


def _mac(body, key):
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(key + b"\x00" + payload.encode("utf-8")).hexdigest()


def stub_verifier(record, key):
    if not isinstance(record, dict) or "signature" not in record:
        return StubVerdict(False, "evidence-shape", "not a sealed record")
    body = {k: v for k, v in record.items() if k != "signature"}
    if _mac(body, key) != record["signature"]:
        return StubVerdict(False, "evidence-signature", "the MAC does not match")
    return StubVerdict(True)


def record(*, component="Classifier", step_index=0, role="cloud",
           residence="off_device", placement=PLACEMENT_CLOUD,
           answer=DIGEST_A, outcome="validated", policy=POLICY,
           binding_value=DIGEST_C, key=KEY):
    candidates = [answer] if outcome == "validated" else []
    chosen = 0 if outcome == "validated" else None
    body = {
        "kind": "revl.model-decision", "version": "1.0",
        "sign_alg": "hmac-sha256", "hash_alg": "sha256",
        "key_id": "0" * 16, "recorded_at": "2026-09-20T00:00:00Z",
        "component": component, "step_index": step_index,
        "role": role, "residence": residence,
        "model_digest": DIGEST_B, "placement_digest": placement,
        "prompt_binding": {"mode": "content-addressed",
                           "value": binding_value, "reason": None},
        "origins": ["input"], "candidates": list(candidates),
        "chosen": chosen, "outcome": outcome,
        "sampling": {"max_tokens": 256, "seed": 7, "stop_digest": None,
                     "temperature": 0.0, "top_k": None, "top_p": 1.0},
        "policy_digest": policy, "fallback_depth": 0, "retained": None,
    }
    sealed = dict(body)
    sealed["signature"] = _mac(body, key)
    return sealed


PERFECT_SLO = {"cost": 0.0, "latency_ms": 1.0, "refusal_rate": 0.0,
               "tokens": 1}


# --------------------------------------------------------------------------
# recorded worlds, in `revl.mcp.canary`'s own format
# --------------------------------------------------------------------------

def timeline(component, steps):
    """A `replay.Timeline` carrying the given `(kind, label, slot)` steps.

    This is the format `revl.mcp.canary` builds and compares, and the `slot`
    detail is item 496's: the half of a step's provenance that is behaviour
    rather than a name. Nothing is reimplemented here; the steps are appended
    with the timeline's own `_add` and compared with the canary's own
    `compare_timelines`."""
    replay = replay_module()
    out = replay.Timeline(component)
    for kind, label, slot in steps:
        out._add(kind, label, None, detail={"origin": component, "slot": slot,
                                            "undo": None, "compensate": None})
    return out


def world_pair(*, relocate=False, extra=False):
    """Two recorded worlds, identical unless asked otherwise.

    `relocate` moves the acquisition from the entry point at slot 0 to the one
    at slot 1 WITHOUT changing any step's kind or label. That is exactly item
    496's finding: a generation that moves a write onto the read path names
    the same completion and records a different world, and before the slot was
    keyed it compared equal."""
    replay = replay_module()
    base = [(replay.KIND_EFFECT, "store.insert(k, v)", 0),
            (replay.KIND_EMISSION, "sink.commit(v)", 0)]
    left = timeline("Classifier", base)
    if relocate:
        right = timeline("Classifier",
                         [(replay.KIND_EFFECT, "store.insert(k, v)", 1),
                          (replay.KIND_EMISSION, "sink.commit(v)", 0)])
    elif extra:
        right = timeline("Classifier", base + [
            (replay.KIND_EFFECT, "store.insert(k2, v)", 0)])
    else:
        right = timeline("Classifier", base)
    return left, right


# --------------------------------------------------------------------------
# the scheduler's seam, and the plan
# --------------------------------------------------------------------------

def route(**overrides):
    base = dict(component="Classifier", action="classify", realm=REALM,
                incumbent_role="local", candidate_role="cloud",
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


def seam(*, diverge_at=None, diverge="answer", worlds=False, slo=None):
    """`(incumbent, candidate, slo)` producers for :func:`revl.shadow_routing.serve`.

    `diverge` says HOW the candidate differs at `diverge_at`: `"answer"`
    changes the completion digest the record names, `"world"` leaves both
    records naming the same completion and relocates a step in the recorded
    world. The second is the case item 496 measured and the one a record-only
    comparison cannot see."""
    def incumbent(crossing):
        world = world_pair()[0] if worlds else None
        return sr.Answered(record(step_index=crossing[1], role="local",
                                  residence="on_device",
                                  placement=PLACEMENT_LOCAL, answer=DIGEST_A),
                           world)

    def candidate(crossing):
        step = crossing[1]
        answer = DIGEST_A
        world = None
        if worlds:
            relocate = diverge_at == step and diverge == "world"
            world = world_pair(relocate=relocate)[1]
        if diverge_at == step and diverge == "answer":
            answer = DIGEST_B
        return sr.Answered(record(step_index=step, role="cloud",
                                  residence="off_device",
                                  placement=PLACEMENT_CLOUD, answer=answer),
                           world)

    metrics = (lambda _crossing: dict(slo)) if slo is not None else None
    return incumbent, candidate, metrics


def run(*, n=20, route_overrides=None, seam_kwargs=None, plan_overrides=None):
    """Serve `n` crossings and decide, returning `(ledger, verdict)`."""
    the_route = route(**(route_overrides or {}))
    incumbent, candidate, metrics = seam(**(seam_kwargs or {}))
    crossings = [("Classifier", i) for i in range(n)]
    ledger = sr.serve(the_route, crossings, incumbent=incumbent,
                      candidate=candidate, slo=metrics)
    the_plan = plan_for(ledger, **(plan_overrides or {}))
    verdict = sr.decide(the_route, the_plan, ledger.entries, key=KEY,
                        verifier=stub_verifier)
    return ledger, verdict


# ==========================================================================
# 1. NON-VACUITY: the promote and the revert both happen, measured
# ==========================================================================

def test_the_same_twenty_pairs_promote_in_shadow_and_revert_when_live():
    """The differential. One window of twenty pairs, one relocated step, and
    the only thing that changes between the two runs is whether the candidate
    is the one answering. Both outcomes are MEASURED here, so neither branch
    is a claim."""
    clean_ledger, clean = run(n=20, seam_kwargs={"worlds": True})
    assert clean.decision == sp.PROMOTE, clean.refusal
    assert clean.tally.paired == 20
    assert clean.tally.agreed == 20
    assert clean.tally.agreement == 1.0
    assert clean_ledger.worlds_recorded == 20

    live_ledger, live = run(
        n=20, route_overrides={"live": True},
        seam_kwargs={"worlds": True, "diverge_at": 7, "diverge": "world"})
    assert live.decision == sp.REVERT, live.refusal
    assert live.tally.paired == 20
    assert live.tally.diverged == 1
    assert live_ledger.worlds_recorded == 20

    assert {clean.decision, live.decision} == {sp.PROMOTE, sp.REVERT}


def test_the_ledger_counts_what_was_served_rather_than_deriving_it():
    """`share = 1/4` over 200 crossings: the candidate is consulted on exactly
    the crossings the share selected and on no others, and the count comes
    from the call site rather than from the share."""
    incumbent, candidate, _ = seam()
    calls = []

    def counting_candidate(crossing):
        calls.append(crossing)
        return candidate(crossing)

    the_route = route(share=sr.Share(1, 4))
    crossings = [("Classifier", i) for i in range(200)]
    ledger = sr.serve(the_route, crossings, incumbent=incumbent,
                      candidate=counting_candidate)
    expected = [c for c in crossings if sr.selects(the_route, c)]
    assert calls == expected
    assert ledger.candidate_calls == len(expected)
    assert ledger.shadowed == len(expected)
    assert ledger.offered == 200
    assert 0 < len(expected) < 200, len(expected)


# ==========================================================================
# 2. ADVERSARIAL 1: agreement on every recorded step promotes
# ==========================================================================

def test_a_shadow_that_agrees_on_every_recorded_step_promotes():
    ledger, verdict = run(n=20, seam_kwargs={"worlds": True})
    assert verdict.decision == sp.PROMOTE
    assert verdict.refusal is None
    assert verdict.measurements_read is True
    assert verdict.tally.divergences == ()
    assert ledger.shadowed == 20


def test_the_promotion_lands_with_the_comparison_attached():
    """The item's own exit clause. A `PROMOTE` carries the tally, and the
    tally is a count of comparison outcomes with no metric in it."""
    _ledger, verdict = run(n=20, seam_kwargs={"worlds": True,
                                              "slo": PERFECT_SLO})
    assert verdict.decision == sp.PROMOTE
    body = verdict.as_dict()
    assert body["tally"]["paired"] == 20
    assert body["tally"]["agreed"] == 20
    assert body["measurements_read"] is True
    assert body["slo_reads"] == 0
    assert body["slo_supplied"] == 20
    json.dumps(body)


# ==========================================================================
# 3. ADVERSARIAL 2: the first divergence is attributed and reverts
# ==========================================================================

def test_a_live_divergence_reverts_and_names_the_component_and_realm():
    _ledger, verdict = run(
        n=20, route_overrides={"live": True},
        seam_kwargs={"worlds": True, "diverge_at": 3, "diverge": "world"})
    assert verdict.decision == sp.REVERT
    assert verdict.refusal.link == sp.DIVERGENCE_ATTRIBUTED
    first = verdict.tally.first_divergence
    assert first.crossing == ("Classifier", 3)
    assert first.realm == REALM
    assert first.attribution == ("Classifier", REALM)
    assert first.member == sp.WORLD_COMPARISON
    assert first.at_step == 0
    assert "Classifier" in verdict.refusal.reason
    assert REALM in verdict.refusal.reason


def test_the_revert_names_the_step_not_only_the_crossing():
    """Item 518's exit says the revert names THE STEP that diverged. The
    record-member comparison can only name a member of a record; the recorded
    world names a replay step index and the two step renderings."""
    _ledger, verdict = run(
        n=8, route_overrides={"live": True},
        seam_kwargs={"worlds": True, "diverge_at": 2, "diverge": "world"})
    first = verdict.tally.first_divergence
    assert first.at_step == 0
    described = first.describe()
    assert "replay step 0" in described
    assert "store.insert(k, v)" in described
    assert f"realm `{REALM}`" in described


def test_a_revert_reports_restored_and_compensated_separately():
    """Item 522's vocabulary, as slice 1 implements it. A `REVERT` from the
    scheduler goes through the same `_restoration`, so the two lists stay two
    lists and no sixth word is introduced here."""
    _ledger, verdict = run(
        n=8, route_overrides={"live": True},
        seam_kwargs={"worlds": True, "diverge_at": 1, "diverge": "world"})
    restoration = verdict.restoration
    assert restoration["restored"] == ["route-arm"]
    assert [e["layer"] for e in restoration["compensated"]] == [
        "agreement-ledger", "placement-history"]
    assert restoration["neither"] == []
    assert "rolled back" not in sr.render(verdict, _ledger)


def test_a_record_member_divergence_is_still_attributed_to_the_realm():
    """The other leg. A candidate that names a different completion diverges
    on `chosen_digest`, and the realm the scheduler stamped travels with it."""
    _ledger, verdict = run(
        n=8, route_overrides={"live": True},
        seam_kwargs={"diverge_at": 5, "diverge": "answer"})
    first = verdict.tally.first_divergence
    assert first.member == "chosen_digest"
    assert first.attribution == ("Classifier", REALM)
    assert first.at_step is None


# ==========================================================================
# 4. ADVERSARIAL 3: a promotion with no accumulated evidence
# ==========================================================================

def test_a_promotion_with_no_accumulated_evidence_refuses():
    ledger, verdict = run(n=0)
    assert ledger.entries == ()
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.EVIDENCE_MISSING
    assert verdict.tally is None
    assert verdict.measurements_read is False


def test_a_share_of_nothing_accumulates_nothing_and_refuses():
    """The share is the off switch, and turning it off must not read as
    agreement. Twenty crossings offered, none shadowed, the candidate never
    consulted, and the promotion refused for absent evidence."""
    ledger, verdict = run(n=20, route_overrides={"share": sr.NOTHING})
    assert ledger.offered == 20
    assert ledger.shadowed == 0
    assert ledger.candidate_calls == 0
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.EVIDENCE_MISSING


def test_a_window_below_the_stated_sample_is_not_a_promotion():
    _ledger, verdict = run(n=3)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.SAMPLE_TOO_SMALL


# ==========================================================================
# 5. ADVERSARIAL 4: another action class's evidence
# ==========================================================================

def test_a_window_of_another_actions_evidence_cannot_meet_this_ones_threshold():
    """The correlation slice 1 does not have, measured.

    `summarize`'s window is twenty perfectly agreeing pairs. Handed to a plan
    promoting `classify` it is refused by the stamp, because the scheduler
    knows which action it scheduled each observation for and item 517's record
    does not."""
    summarize_route = route(action="summarize")
    incumbent, candidate, _ = seam()
    crossings = [("Classifier", i) for i in range(20)]
    other = sr.serve(summarize_route, crossings, incumbent=incumbent,
                     candidate=candidate)
    assert len(other.entries) == 20

    classify_route = route(action="classify")
    the_plan = plan_for(sr.ShadowLedger(route=classify_route),
                        route_table=route_table(),
                        authority_diff=dict(EMPTY_DIFF), layers=dict(LAYERS),
                        compensations={k: dict(v)
                                       for k, v in COMPENSATIONS.items()},
                        threshold=0.95, min_observations=4)
    verdict = sr.decide(classify_route, the_plan, other.entries, key=KEY,
                        verifier=stub_verifier)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sr.ACTION_MISMATCHED
    assert verdict.tally is None
    assert "summarize" in verdict.refusal.reason
    assert "classify" in verdict.refusal.reason


def test_the_same_window_under_its_own_action_promotes():
    """The control for the case above, on the same twenty pairs. Without it
    the refusal could be a refusal of everything."""
    summarize_route = route(action="summarize")
    incumbent, candidate, _ = seam()
    crossings = [("Classifier", i) for i in range(20)]
    other = sr.serve(summarize_route, crossings, incumbent=incumbent,
                     candidate=candidate)
    verdict = sr.decide(summarize_route, plan_for(other), other.entries,
                        key=KEY, verifier=stub_verifier)
    assert verdict.decision == sp.PROMOTE
    assert verdict.action_class == ("Classifier", "summarize")


def test_slice_one_alone_cannot_tell_the_two_windows_apart():
    """The measurement that says the stamp is load-bearing rather than
    decorative: the SAME twenty observations, handed to `shadow_promotion`
    without the schedule, promote `classify` on `summarize`'s evidence.

    This is `docs/design/540-shadow-promotion.md` section 7's own stated gap,
    run rather than quoted. It is not a defect in slice 1: item 517's record
    body is a closed vocabulary that names no action, so the correlation has
    to come from the scheduler."""
    summarize_route = route(action="summarize")
    incumbent, candidate, _ = seam()
    crossings = [("Classifier", i) for i in range(20)]
    other = sr.serve(summarize_route, crossings, incumbent=incumbent,
                     candidate=candidate)

    classify_plan = sr.ShadowLedger(route=route(action="classify")).plan(
        route_table=route_table(), authority_diff=dict(EMPTY_DIFF),
        layers=dict(LAYERS),
        compensations={k: dict(v) for k, v in COMPENSATIONS.items()},
        threshold=0.95, min_observations=4)
    unstamped = sp.decide(classify_plan, other.observations, key=KEY,
                          verifier=stub_verifier)
    assert unstamped.decision == sp.PROMOTE
    assert unstamped.action_class == ("Classifier", "classify")

    stamped = sr.decide(route(action="classify"), classify_plan,
                        other.entries, key=KEY, verifier=stub_verifier)
    assert stamped.decision == sp.REFUSE
    assert stamped.refusal.link == sr.ACTION_MISMATCHED


def test_an_observation_nothing_scheduled_refuses_rather_than_counting():
    """A bare `Observation` handed in beside the stamped ones. It carries no
    action, so nothing says which window it belongs in, and an unstamped
    observation is refused rather than admitted on the strength of its
    neighbours."""
    ledger, _verdict = run(n=20)
    bare = ledger.entries[0].observation
    mixed = list(ledger.entries) + [bare]
    verdict = sr.decide(route(), plan_for(ledger), mixed, key=KEY,
                        verifier=stub_verifier)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sr.ACTION_UNSCHEDULED


def test_a_window_served_in_another_realm_refuses():
    ledger, _verdict = run(n=20)
    moved = [sr.Entry(crossing=e.crossing, component=e.component,
                      action=e.action, realm=OTHER_REALM, side=e.side,
                      observation=e.observation)
             for e in ledger.entries]
    verdict = sr.decide(route(), plan_for(ledger), moved, key=KEY,
                        verifier=stub_verifier)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sr.REALM_MISMATCHED


def test_a_schedule_for_another_promotion_cannot_supply_this_one():
    ledger, _verdict = run(n=20)
    verdict = sr.decide(route(candidate_role="elsewhere"), plan_for(ledger),
                        ledger.entries, key=KEY, verifier=stub_verifier)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sr.ROUTE_MISMATCHED


# ==========================================================================
# 6. ADVERSARIAL 5: the metric would promote, the replay would not
# ==========================================================================

def test_a_perfect_metric_does_not_survive_a_diverging_replay():
    """The load-bearing one. Both records name the SAME completion and the
    SAME outcome, every observation carries a perfect SLO block, and the two
    recorded worlds differ by one relocated step. The replay comparison wins,
    and the metric was read zero times getting there."""
    _ledger, verdict = run(
        n=20, route_overrides={"live": True},
        seam_kwargs={"worlds": True, "diverge_at": 11, "diverge": "world",
                     "slo": PERFECT_SLO})
    assert verdict.decision == sp.REVERT
    assert verdict.slo_supplied == 20
    assert verdict.slo_reads == 0
    first = verdict.tally.first_divergence
    assert first.member == sp.WORLD_COMPARISON
    assert first.at_field == "slot"
    # the two sides agree on kind and label and are still told apart, which is
    # the shape item 496 was filed about
    assert first.incumbent == "effect store.insert(k, v) [slot=0]"
    assert first.candidate == "effect store.insert(k, v) [slot=1]"


def test_the_ratio_meets_the_threshold_and_the_attributed_divergence_still_wins():
    """The clearest statement that this is not a numeric flip.

    Nineteen of twenty pairs agree, the stated threshold is 0.95, and the
    accumulated ratio is EXACTLY 0.95. A gate that compared a number against a
    number would promote. The attributed divergence reverts it instead, and
    the report carries the ratio beside the attribution rather than in place
    of it."""
    _ledger, verdict = run(
        n=20, route_overrides={"live": True},
        seam_kwargs={"worlds": True, "diverge_at": 11, "diverge": "world",
                     "slo": PERFECT_SLO},
        plan_overrides={"threshold": 0.95})
    assert verdict.tally.agreement == 0.95
    assert verdict.tally.agreement >= verdict.threshold
    assert verdict.decision == sp.REVERT
    assert verdict.refusal.link == sp.DIVERGENCE_ATTRIBUTED


def test_the_records_really_do_agree_on_every_member_of_that_window():
    """The control that makes the test above mean what it says. If the two
    records differed on `chosen_digest` the verdict would be explained by the
    record comparison and the recorded world would be doing nothing."""
    incumbent, candidate, _ = seam(worlds=True, diverge_at=11,
                                   diverge="world")
    left = incumbent(("Classifier", 11))
    right = candidate(("Classifier", 11))
    for member in ("candidates", "chosen", "outcome"):
        assert left.record[member] == right.record[member]
    assert canary.compare_timelines(left.world, right.world)["diverged"] is True


def test_the_same_window_without_the_recorded_worlds_promotes():
    """The measured differential for the recorded-world leg, and item 496's
    own finding reproduced one layer up: strip the two worlds and the same
    twenty pairs promote, because the records never disagreed."""
    _ledger, without = run(
        n=20, route_overrides={"live": True},
        seam_kwargs={"diverge_at": 11, "diverge": "world",
                     "slo": PERFECT_SLO})
    assert without.decision == sp.PROMOTE
    assert without.tally.agreement == 1.0

    _ledger2, with_worlds = run(
        n=20, route_overrides={"live": True},
        seam_kwargs={"worlds": True, "diverge_at": 11, "diverge": "world",
                     "slo": PERFECT_SLO})
    assert with_worlds.decision == sp.REVERT


def test_a_longer_candidate_world_is_a_divergence_too():
    """Canary's length-mismatch branch, reached through the scheduler. The
    candidate records one step the incumbent never did."""
    left, right = world_pair(extra=True)
    observation = sp.Observation(
        record(step_index=0, role="local", residence="on_device",
               placement=PLACEMENT_LOCAL),
        record(step_index=0, role="cloud", residence="off_device",
               placement=PLACEMENT_CLOUD),
        realm=REALM, incumbent_world=left, candidate_world=right)
    pairs, refusal = sp.admit(
        route().__class__ and plan_for(sr.ShadowLedger(route=route())),
        [observation], key=KEY, verifier=stub_verifier)
    assert refusal is None, refusal
    tally = sp.accumulate(("Classifier", "classify"), pairs)
    assert tally.diverged == 1
    assert tally.first_divergence.member == sp.WORLD_COMPARISON
    assert tally.first_divergence.candidate != "absent"
    assert tally.first_divergence.incumbent == "absent"


def test_an_unavailable_comparator_diverges_rather_than_agreeing():
    """Fail-closed on the comparator itself. "The comparison did not run" and
    "the two worlds matched" are the two branches item 496 measured the cost
    of confusing, so an unresolvable comparator is a divergence."""
    left, right = world_pair()
    observation = sp.Observation(
        record(step_index=0, role="local", residence="on_device",
               placement=PLACEMENT_LOCAL),
        record(step_index=0, role="cloud", residence="off_device",
               placement=PLACEMENT_CLOUD),
        realm=REALM, incumbent_world=left, candidate_world=right)
    pairs, refusal = sp.admit(plan_for(sr.ShadowLedger(route=route())),
                              [observation], key=KEY, verifier=stub_verifier)
    assert refusal is None
    assert sp.accumulate(("Classifier", "classify"), pairs).agreed == 1
    blind = sp.accumulate(("Classifier", "classify"), pairs,
                          _resolve=lambda: None)
    assert blind.agreed == 0
    assert blind.first_divergence.member == sp.WORLD_COMPARISON


# ==========================================================================
# 7. the served answer is the incumbent's
# ==========================================================================

def test_in_shadow_every_answer_served_is_the_incumbents():
    """Measured over a window in which the candidate's answers really do
    differ, so "the incumbent answered" is not true by the candidate being a
    copy."""
    _ledger, _verdict = run(n=20)
    the_route = route()
    incumbent, candidate, _ = seam(diverge_at=4, diverge="answer")
    crossings = [("Classifier", i) for i in range(20)]
    ledger = sr.serve(the_route, crossings, incumbent=incumbent,
                      candidate=candidate)
    assert {s.side for s in ledger.served} == {sr.INCUMBENT}
    assert all(e.side == sr.INCUMBENT for e in ledger.entries)
    assert ledger.candidate_calls == 20


def test_a_candidate_that_answered_in_shadow_refuses():
    ledger, _verdict = run(n=20)
    cut_over = [sr.Entry(crossing=e.crossing, component=e.component,
                         action=e.action, realm=e.realm, side=sr.CANDIDATE,
                         observation=e.observation)
                for e in ledger.entries]
    verdict = sr.decide(route(), plan_for(ledger), cut_over, key=KEY,
                        verifier=stub_verifier)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sr.SERVED_CANDIDATE


def test_a_live_route_serves_the_candidate_and_that_is_not_a_refusal():
    ledger, verdict = run(n=20, route_overrides={"live": True})
    assert {s.side for s in ledger.served} == {sr.CANDIDATE}
    assert verdict.decision == sp.PROMOTE


# ==========================================================================
# 8. the share: deterministic, replayable, and not a verdict input
# ==========================================================================

@pytest.mark.parametrize("share,expected", [
    (sr.NOTHING, 0), (sr.EVERYTHING, 200), (sr.Share(1, 2), None),
])
def test_the_two_ends_of_the_share_are_exact(share, expected):
    the_route = route(share=share)
    crossings = [("Classifier", i) for i in range(200)]
    selected = sum(1 for c in crossings if sr.selects(the_route, c))
    if expected is None:
        assert 0 < selected < 200
    else:
        assert selected == expected


def test_the_selection_is_a_pure_function_of_the_crossing():
    """Order-independent and repeatable, which is what makes a shadow window
    reproducible from a replay rather than only from a log of what happened to
    be sampled."""
    the_route = route(share=sr.Share(3, 7))
    crossings = [("Classifier", i) for i in range(300)]
    forward = {c for c in crossings if sr.selects(the_route, c)}
    backward = {c for c in reversed(crossings) if sr.selects(the_route, c)}
    assert forward == backward
    assert forward == {c for c in crossings if sr.selects(the_route, c)}


def test_a_different_salt_selects_a_different_set():
    """The salt pins the selection, so it has to be able to move it."""
    left = route(share=sr.Share(1, 3), salt="one")
    right = route(share=sr.Share(1, 3), salt="two")
    crossings = [("Classifier", i) for i in range(300)]
    assert {c for c in crossings if sr.selects(left, c)} \
        != {c for c in crossings if sr.selects(right, c)}


def test_the_share_is_not_a_member_of_the_plan_or_the_verdict():
    """The structural half. A share cannot buy a promotion because there is
    nowhere in the gate's inputs to put one."""
    assert "share" not in sp.ShadowPlan.__dataclass_fields__
    assert "share" not in sp.Promotion.__dataclass_fields__
    assert "share" not in sp.Pair.__dataclass_fields__
    _ledger, verdict = run(n=20, route_overrides={"share": sr.Share(1, 1)})
    assert "share" not in json.dumps(verdict.as_dict())


def test_two_shares_over_the_same_crossings_reach_the_same_verdict():
    """A promotion is not bought by observing more. The share changes how many
    pairs were accumulated; it does not change what agreement means, and both
    windows here clear the same stated threshold."""
    whole, whole_verdict = run(n=40)
    part_route = route(share=sr.Share(1, 2))
    incumbent, candidate, _ = seam()
    crossings = [("Classifier", i) for i in range(40)]
    part = sr.serve(part_route, crossings, incumbent=incumbent,
                    candidate=candidate)
    part_verdict = sr.decide(part_route, plan_for(part), part.entries,
                             key=KEY, verifier=stub_verifier)
    assert whole_verdict.decision == part_verdict.decision == sp.PROMOTE
    assert whole.shadowed == 40
    assert part.shadowed < 40
    assert whole_verdict.tally.agreement == part_verdict.tally.agreement == 1.0


@pytest.mark.parametrize("share", [
    sr.Share(2, 1), sr.Share(-1, 4), sr.Share(1, 0), sr.Share(1.0, 4),
])
def test_a_share_that_is_not_a_rational_refuses(share):
    the_route = route(share=share)
    ledger = sr.serve(the_route, [("Classifier", 0)],
                      incumbent=lambda c: sr.Answered(record()),
                      candidate=lambda c: sr.Answered(record()))
    assert ledger.refusal[0] == sr.SHARE_MALFORMED
    assert ledger.entries == ()
    verdict = sr.decide(the_route, plan_for(ledger), [], key=KEY,
                        verifier=stub_verifier)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sr.SHARE_MALFORMED


# ==========================================================================
# 9. structure: one verdict, one vocabulary, no metric
# ==========================================================================

def module_ast():
    return ast.parse(MODULE_PATH.read_text(encoding="utf-8"),
                     feature_version=(3, 11))


def function_named(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined in {MODULE_PATH.name}")


def test_the_scheduler_reads_no_metric_anywhere():
    """No function in this module reads an SLO member. The metrics producer is
    called to BUILD an observation and its value goes straight into the
    `Observation`, which is the only place a metric is allowed to be."""
    tree = module_ast()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in sp.SLO_MEMBERS + ("_slo",), node.attr


def test_the_scheduler_builds_no_verdict_of_its_own():
    """Every decision word a caller can see is `shadow_promotion`'s. A second
    verdict type is how a gate ends up with a fourth outcome nobody gated."""
    assert sr.DECISIONS is sp.DECISIONS
    tree = module_ast()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            assert node.name not in ("Promotion", "Tally", "Divergence")
    for word in ("PROMOTE", "REVERT", "REFUSE"):
        assert not hasattr(sr, word), \
            f"{word} is re-declared here instead of being imported"


def test_the_schedule_checks_read_the_stamp_and_never_an_answer():
    """The scheduler's own checks sit on the same side of the barrier as the
    gate's preconditions, asserted on the source rather than on a comment."""
    answer_reads = ("incumbent_answer", "candidate_answer", "_incumbent",
                    "_candidate", "_incumbent_world", "_candidate_world",
                    "chosen", "candidates", "outcome", "slo")
    for name in ("_check_entries", "_check_route", "selects", "_draw"):
        node = function_named(module_ast(), name)
        for child in ast.walk(node):
            if isinstance(child, ast.Attribute):
                assert child.attr not in answer_reads, \
                    f"{name} reads {child.attr}, which is an answer member"


def test_the_scheduler_runs_before_the_gate():
    """`decide` returns on the first schedule refusal, and the single call to
    `promotion.decide` is after every one of them."""
    node = function_named(module_ast(), "decide")
    calls = [n for n in ast.walk(node)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "decide"]
    assert len(calls) == 1
    refusals = [n for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "refused"]
    assert refusals, "the schedule must be able to refuse"
    assert all(r.lineno < calls[0].lineno for r in refusals)


def test_every_link_this_module_declares_is_reachable():
    """No link is decoration. Each one is produced by a real run."""
    reached = set()

    ledger, _ = run(n=20)
    good_plan = plan_for(ledger)

    bad_share = route(share=sr.Share(3, 2))
    reached.add(sr.decide(bad_share, good_plan, [], key=KEY,
                          verifier=stub_verifier).refusal.link)
    reached.add(sr.decide("not a route", good_plan, [], key=KEY,
                          verifier=stub_verifier).refusal.link)
    reached.add(sr.decide(route(action="summarize"), good_plan,
                          ledger.entries, key=KEY,
                          verifier=stub_verifier).refusal.link)
    other = sr.serve(route(action="summarize"),
                     [("Classifier", i) for i in range(4)],
                     incumbent=seam()[0], candidate=seam()[1])
    reached.add(sr.decide(route(), good_plan, other.entries, key=KEY,
                          verifier=stub_verifier).refusal.link)
    reached.add(sr.decide(route(), good_plan,
                          [ledger.entries[0].observation], key=KEY,
                          verifier=stub_verifier).refusal.link)
    moved = [sr.Entry(e.crossing, e.component, e.action, OTHER_REALM, e.side,
                      e.observation) for e in ledger.entries]
    reached.add(sr.decide(route(), good_plan, moved, key=KEY,
                          verifier=stub_verifier).refusal.link)
    cut = [sr.Entry(e.crossing, e.component, e.action, e.realm, sr.CANDIDATE,
                    e.observation) for e in ledger.entries]
    reached.add(sr.decide(route(), good_plan, cut, key=KEY,
                          verifier=stub_verifier).refusal.link)

    assert reached == set(sr.LINKS), set(sr.LINKS) - reached


def test_a_schedule_refusal_reaches_no_stage_of_the_gate():
    ledger, _ = run(n=20)
    verdict = sr.decide(route(action="summarize"), plan_for(ledger),
                        ledger.entries, key=KEY, verifier=stub_verifier)
    stages = {entry["stage"]: entry["status"]
              for entry in verdict.preconditions}
    assert stages[sr.SCHEDULE_STAGE] == "refused"
    for stage in sp.STAGES:
        assert stages[stage] == "not reached"
    assert verdict.measurements_read is False


def test_the_module_registers_no_guarantee_code():
    for link in sr.LINKS:
        assert not link.startswith("G-"), link
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "GUARANTEES" not in source
    assert "register_guarantee" not in source


def test_the_report_states_the_schedule_before_the_verdict():
    ledger, verdict = run(n=20, route_overrides={"share": sr.Share(1, 2)})
    text = sr.render(verdict, ledger)
    assert text.index("schedule:") < text.index("decision:")
    assert f"share {ledger.route.share}" in text
    assert f"realm `{REALM}`" in text
    assert "candidate consulted:" in text


# ==========================================================================
# 10. integration with the modules this one is built on
# ==========================================================================

def test_the_crossing_key_is_item_517s_own():
    """The scheduler invents no second correlation for the CROSSING; the
    action and the realm are the only things it adds, and it adds them because
    item 517's body is closed and names neither."""
    from revl import model_evidence

    sealed = record(step_index=9)
    assert model_evidence.crossing_key(sealed) == ("Classifier", 9)
    assert "action" not in model_evidence.BODY_MEMBERS
    assert "realm" not in model_evidence.BODY_MEMBERS


def test_a_window_of_really_sealed_records_promotes():
    """End to end against `revl.model_evidence`'s real sealer and verifier,
    so the record shape this file builds cannot drift from the shipped one."""
    from revl import model_evidence

    key = b"k" * 32
    entries = []
    for step in range(8):
        sides = []
        for role, residence, placement in (
                ("local", "on_device", PLACEMENT_LOCAL),
                ("cloud", "off_device", PLACEMENT_CLOUD)):
            sides.append(model_evidence.seal(
                key, component="Classifier", step_index=step, role=role,
                residence=residence, model_digest=DIGEST_B,
                placement_digest=placement,
                prompt_binding={"mode": "content-addressed",
                                "value": DIGEST_C, "reason": None},
                origins=["input"], candidates=[DIGEST_A], chosen=0,
                outcome="validated",
                sampling={"max_tokens": 256, "seed": 7, "stop_digest": None,
                          "temperature": 0.0, "top_k": None, "top_p": 1.0},
                policy_digest=POLICY, fallback_depth=0, retained=None,
                recorded_at="2026-09-20T00:00:00Z"))
        entries.append(sr.Entry(
            crossing=("Classifier", step), component="Classifier",
            action="classify", realm=REALM, side=sr.INCUMBENT,
            observation=sp.Observation(sides[0], sides[1], realm=REALM)))

    the_route = route()
    ledger = sr.ShadowLedger(route=the_route, entries=tuple(entries))
    verdict = sr.decide(the_route, plan_for(ledger), entries, key=key)
    assert verdict.decision == sp.PROMOTE, verdict.refusal
    assert verdict.tally.paired == 8


def test_the_comparison_is_the_canarys_own_function():
    """Not a restatement of it. If `canary.compare_timelines` changes its key,
    this leg changes with it, which is the point of calling it."""
    assert sp._resolve_comparator() is canary.compare_timelines

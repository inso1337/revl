"""Shadow routing: the agreement accumulator and the promotion gate
(roadmap item 518, issue #1192).

The executable spec for `docs/design/539-shadow-promotion.md`. Four things it
is here to establish, in the order the design note argues them:

1. **Non-vacuity.** The gate refuses a promotion whose evidence is absent or
   unverifiable, refuses one whose authority diff is non-empty on ANY axis,
   promotes a complete one, and reverts a live one on the first attributed
   divergence.
2. **The barrier holds.** A promotion with a perfect agreement window AND a
   perfect SLO block is still refused by a non-empty authority diff, the
   verdict reports `measurements_read` false with no tally built, and the
   observations' metric blocks were read ZERO times on every path.
3. **The barrier is structural, not a convention.** An AST walk over the
   module asserts that no precondition reads an answer member and that no
   function but the `Observation` property reads `slo`.
4. **Restore is not compensate.** A revert reports the two separately and
   `render` never spells either "rolled back".

The item 517 records are built here rather than imported from
`revl.model_evidence`, because that module arrives with item 517 and this file
must COLLECT and PASS on a tree that does not have it yet. The two
integration tests that do use it skip when it is absent, and they are what
holds the record shape and the crossing key from drifting.
"""

import ast
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import parser as revl_parser  # noqa: E402
from revl import model_route  # noqa: E402
from revl import shadow_promotion as sp  # noqa: E402

MODULE_PATH = ROOT / "src" / "revl" / "shadow_promotion.py"

KEY = b"shadow-promotion-test-key"

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64

POLICY = "e" * 64
PLACEMENT_LOCAL = "1" * 64
PLACEMENT_CLOUD = "2" * 64


# --------------------------------------------------------------------------
# the route table, derived from a real program by item 512's own checker
# --------------------------------------------------------------------------

PROGRAM = """
model role local on_device
model role cloud off_device
model role elsewhere off_device

service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  route model on classify {
    confidential -> local,
    * -> cloud
  }
  provide out { fn classify(text) = text }
}
"""


def route_table():
    program = revl_parser.Parser(PROGRAM, "m.rvl").parse()
    return model_route.check(program)


# --------------------------------------------------------------------------
# item 517 records, and a verifier stub with its shape
# --------------------------------------------------------------------------

class StubVerdict:
    """The `.ok / .link / .reason` shape `revl.model_evidence.Verdict` has."""

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
           binding_mode="content-addressed", binding_value=DIGEST_C,
           candidates=None, chosen=None, key=KEY, **overrides):
    """One item 517 shaped, signed model-decision record.

    `answer` is the completion digest the decision TOOK; the candidate list and
    the chosen index are derived from it so the pair `candidates[chosen]` says
    what the model said, which is what `Pair._answer` reads."""
    if candidates is None:
        candidates = [answer] if outcome == "validated" else []
    if chosen is None and outcome == "validated":
        chosen = candidates.index(answer)
    body = {
        "kind": "revl.model-decision",
        "version": "1.0",
        "sign_alg": "hmac-sha256",
        "hash_alg": "sha256",
        "key_id": "0" * 16,
        "recorded_at": "2026-09-20T00:00:00Z",
        "component": component,
        "step_index": step_index,
        "role": role,
        "residence": residence,
        "model_digest": DIGEST_B,
        "placement_digest": placement,
        "prompt_binding": {"mode": binding_mode, "value": binding_value,
                           "reason": None},
        "origins": ["input"],
        "candidates": list(candidates),
        "chosen": chosen,
        "outcome": outcome,
        "sampling": {"max_tokens": 256, "seed": 7, "stop_digest": None,
                     "temperature": 0.0, "top_k": None, "top_p": 1.0},
        "policy_digest": policy,
        "fallback_depth": 0,
        "retained": None,
    }
    body.update(overrides)
    sealed = dict(body)
    sealed["signature"] = _mac(body, key)
    return sealed


PERFECT_SLO = {"cost": 0.0, "latency_ms": 1.0, "refusal_rate": 0.0,
               "tokens": 1}


_KEEP = object()


def observation(step, *, incumbent_answer=DIGEST_A, candidate_answer=None,
                slo=PERFECT_SLO, placement=_KEEP, **kwargs):
    """One agreeing shadow pair unless `candidate_answer` says otherwise.

    `placement` overrides BOTH sides' placement digest, which is how a test
    says "the host profile was never pinned"."""
    candidate_answer = incumbent_answer if candidate_answer is None \
        else candidate_answer
    local = PLACEMENT_LOCAL if placement is _KEEP else placement
    cloud = PLACEMENT_CLOUD if placement is _KEEP else placement
    incumbent = record(
        step_index=step, role="local", residence="on_device",
        placement=local, answer=incumbent_answer, **kwargs)
    candidate = record(
        step_index=step, role="cloud", residence="off_device",
        placement=cloud, answer=candidate_answer, **kwargs)
    return sp.Observation(incumbent, candidate, slo=slo)


def window(n=8, diverge_at=None, diverge_outcome=False):
    out = []
    for step in range(n):
        if diverge_at is not None and step == diverge_at:
            if diverge_outcome:
                out.append(sp.Observation(
                    record(step_index=step, role="local",
                           residence="on_device", placement=PLACEMENT_LOCAL,
                           answer=DIGEST_A),
                    record(step_index=step, role="cloud",
                           residence="off_device", placement=PLACEMENT_CLOUD,
                           outcome="refused", candidates=[], chosen=None),
                    slo=PERFECT_SLO))
            else:
                out.append(observation(step, candidate_answer=DIGEST_B))
        else:
            out.append(observation(step))
    return out


EMPTY_DIFF = {axis: [] for axis in sp.AUTHORITY_AXES}

REVERTIBLE_LAYERS = {
    "route-arm": "revertible",
    "agreement-ledger": "compensatable",
    "placement-history": "neither",
}

COMPENSATIONS = {
    "agreement-ledger": {"name": "supersede-window", "tested": True},
    "placement-history": {"name": "replay-placement-log", "tested": True},
}


def plan(**overrides):
    base = dict(
        component="Classifier", action="classify",
        incumbent_role="local", candidate_role="cloud",
        route_table=route_table(),
        authority_diff=dict(EMPTY_DIFF),
        layers=dict(REVERTIBLE_LAYERS),
        compensations={k: dict(v) for k, v in COMPENSATIONS.items()},
        threshold=0.95, min_observations=4, live=False,
    )
    base.update(overrides)
    return sp.ShadowPlan(**base)


def decide(p=None, obs=None, **kwargs):
    return sp.decide(p if p is not None else plan(),
                     obs if obs is not None else window(),
                     key=KEY, verifier=stub_verifier, **kwargs)


# --------------------------------------------------------------------------
# the control: identical on a tree with and without this change
# --------------------------------------------------------------------------

def test_the_route_table_this_gate_reads_is_item_512s_unchanged():
    """The CONTROL. This item adds no placement channel and changes no
    compiler behaviour, so `model_route.check()` on the program above returns
    exactly what it returns on a tree without `shadow_promotion.py` at all.
    A red here is the harness, not the gate."""
    assert route_table() == {
        "Classifier": {
            "classify": {
                "confidential": {"role": "local", "residence": "on_device"},
                "*": {"role": "cloud", "residence": "off_device"},
            }
        }
    }


def test_the_module_registers_no_guarantee_code():
    """Refusals here are named links, the `revl.deploy` discipline, and not
    G-codes. Item 523's generated tier matrix requires every G-code to carry a
    reproducer under `examples/rejections/` or an ACKNOWLEDGED entry in
    `tools/tier_guarantees.py`; this module registers none, so it owes
    neither."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "G-MODEL-PLACE" in source, "the note cites item 512's code in prose"
    for link in sp.LINKS:
        assert not link.startswith("G-"), link
        assert link.islower(), link


# --------------------------------------------------------------------------
# 1. non-vacuity: the gate promotes a complete promotion
# --------------------------------------------------------------------------

def test_a_complete_promotion_is_promoted():
    verdict = decide()
    assert verdict.decision == sp.PROMOTE
    assert verdict.refusal is None
    assert verdict.tally.paired == 8
    assert verdict.tally.agreed == 8
    assert verdict.tally.agreement == 1.0
    assert verdict.tally.action_class == ("Classifier", "classify")
    assert [entry["status"] for entry in verdict.preconditions] == \
        ["satisfied"] * len(sp.STAGES)
    assert [entry["stage"] for entry in verdict.preconditions] == \
        list(sp.STAGES)


def test_a_promotion_reads_no_metric_even_when_it_promotes():
    obs = window()
    verdict = sp.decide(plan(), obs, key=KEY, verifier=stub_verifier)
    assert verdict.decision == sp.PROMOTE
    assert verdict.slo_supplied == 8, "every observation carried an SLO block"
    assert verdict.slo_reads == 0
    assert sum(o.slo_reads for o in obs) == 0


def test_agreement_is_a_ratio_over_a_comparison_not_a_metric():
    tally = decide(obs=window(10, diverge_at=3)).tally
    assert tally.paired == 10
    assert tally.agreed == 9
    assert tally.diverged == 1
    assert tally.agreement == pytest.approx(0.9)
    assert "latency" not in json.dumps(tally.as_dict())


# --------------------------------------------------------------------------
# 1b. non-vacuity: absent and unusable evidence
# --------------------------------------------------------------------------

def test_an_empty_window_is_the_absence_of_evidence():
    verdict = decide(obs=[])
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.EVIDENCE_MISSING
    assert verdict.tally is None
    assert verdict.measurements_read is False


def test_no_verifier_refuses_rather_than_admitting_an_unchecked_record():
    """Fail-closed. `revl.model_evidence` arriving with item 517 is what
    verifies these records; its absence may not be a skipped check."""
    verdict = sp.decide(plan(), window(), key=KEY, verifier=None,
                        )
    if verdict.refusal is not None and \
            verdict.refusal.link == sp.EVIDENCE_UNVERIFIABLE:
        return
    # `revl.model_evidence` IS importable on this tree, so the default
    # resolver found a real verifier and refused the stub-signed records.
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link in (sp.EVIDENCE_UNVERIFIED,
                                    sp.EVIDENCE_UNVERIFIABLE)


def test_a_verifier_with_no_key_refuses():
    verdict = sp.decide(plan(), window(), key=None, verifier=stub_verifier)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.EVIDENCE_UNVERIFIABLE


def test_a_tampered_record_refuses():
    obs = window()
    obs[2].candidate["fallback_depth"] = 9
    verdict = decide(obs=obs)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.EVIDENCE_UNVERIFIED
    assert verdict.refusal.where == ("Classifier", 2)


def test_a_verifier_that_raises_is_a_refusal_not_a_pass():
    def explodes(record_, key_):
        raise RuntimeError("boom")

    verdict = sp.decide(plan(), window(), key=KEY, verifier=explodes)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.EVIDENCE_UNVERIFIED


def test_a_pair_that_is_two_different_crossings_refuses():
    obs = window()
    obs[1] = sp.Observation(
        record(step_index=1, role="local", residence="on_device",
               placement=PLACEMENT_LOCAL),
        record(step_index=99, role="cloud", placement=PLACEMENT_CLOUD))
    verdict = decide(obs=obs)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.CROSSING_MISMATCHED


def test_one_crossing_counted_twice_cannot_inflate_the_sample():
    obs = [observation(0), observation(0), observation(0), observation(0),
           observation(0)]
    verdict = decide(obs=obs)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.CROSSING_MISMATCHED
    assert verdict.tally is None


def test_a_pair_recorded_the_other_way_round_refuses():
    obs = window()
    obs[0] = sp.Observation(
        record(step_index=0, role="cloud", placement=PLACEMENT_CLOUD),
        record(step_index=0, role="local", residence="on_device",
               placement=PLACEMENT_LOCAL))
    verdict = decide(obs=obs)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.ROLE_MISPLACED


def test_a_suppressed_prompt_binding_is_unusable_not_agreeing():
    """Item 517 suppresses the binding when the input carried a confidential
    or secret origin. That is a legitimate record and an unusable comparison:
    nothing witnesses that the two worlds were asked the same question. It
    must not count as agreement."""
    obs = window()
    obs[4] = sp.Observation(
        record(step_index=4, role="local", residence="on_device",
               placement=PLACEMENT_LOCAL, binding_mode="suppressed",
               binding_value=None),
        record(step_index=4, role="cloud", placement=PLACEMENT_CLOUD,
               binding_mode="suppressed", binding_value=None))
    verdict = decide(obs=obs)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.BINDING_SUPPRESSED
    assert verdict.tally is None


def test_two_answers_to_two_questions_are_not_agreement():
    obs = window()
    obs[3] = sp.Observation(
        record(step_index=3, role="local", residence="on_device",
               placement=PLACEMENT_LOCAL, binding_value=DIGEST_A),
        record(step_index=3, role="cloud", placement=PLACEMENT_CLOUD,
               binding_value=DIGEST_B))
    verdict = decide(obs=obs)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.BINDING_INCOMPARABLE


def test_an_unknown_binding_mode_refuses():
    obs = window()
    obs[0] = sp.Observation(
        record(step_index=0, role="local", residence="on_device",
               placement=PLACEMENT_LOCAL, binding_mode="whatever"),
        record(step_index=0, role="cloud", placement=PLACEMENT_CLOUD,
               binding_mode="whatever"))
    verdict = decide(obs=obs)
    assert verdict.refusal.link == sp.BINDING_INCOMPARABLE


def test_a_window_accumulated_across_a_policy_change_is_two_windows():
    obs = window()
    obs[5] = observation(5, policy="d" * 64)
    verdict = decide(obs=obs)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.POLICY_DRIFT
    assert verdict.refusal.where == ("Classifier", 5)


def test_a_window_with_no_policy_digest_refuses():
    verdict = decide(obs=[observation(i, policy=None) for i in range(5)])
    assert verdict.refusal.link == sp.POLICY_DRIFT


def test_an_unpinned_placement_profile_refuses():
    obs = window()
    obs[6] = sp.Observation(
        record(step_index=6, role="local", residence="on_device",
               placement=PLACEMENT_LOCAL),
        record(step_index=6, role="cloud", placement="9" * 64))
    verdict = decide(obs=obs)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.PLACEMENT_UNPINNED


def test_a_missing_placement_digest_refuses():
    obs = [observation(i, placement=None) for i in range(5)]
    verdict = decide(obs=obs)
    assert verdict.refusal.link == sp.PLACEMENT_UNPINNED


# --------------------------------------------------------------------------
# 1c. non-vacuity: the route table gate (docs/design/531 section 9)
# --------------------------------------------------------------------------

def test_a_candidate_role_the_action_does_not_route_to_refuses():
    """`elsewhere` is a DECLARED model role of the program and is named by no
    arm of `route model on classify`. Promoting to it would be a second
    placement channel beside item 512's."""
    verdict = decide(plan(candidate_role="elsewhere"))
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.ROUTE_NOT_ROUTABLE
    assert "elsewhere" in verdict.refusal.reason
    assert verdict.tally is None


def test_an_action_with_no_route_block_has_no_placement_to_move_between():
    verdict = decide(plan(action="summarise"))
    assert verdict.refusal.link == sp.ROUTE_UNKNOWN_ACTION


def test_a_component_with_no_route_block_refuses():
    verdict = decide(plan(component="Elsewhere"))
    assert verdict.refusal.link == sp.ROUTE_UNKNOWN_ACTION


def test_promoting_a_role_to_itself_moves_nothing():
    verdict = decide(plan(candidate_role="local"))
    assert verdict.refusal.link == sp.ROLES_IDENTICAL


# --------------------------------------------------------------------------
# 2. THE BARRIER. Issue #1222: no measurement buys a non-empty authority diff
# --------------------------------------------------------------------------

@pytest.mark.parametrize("axis", sp.AUTHORITY_AXES)
def test_no_amount_of_evidence_buys_a_non_empty_authority_diff(axis):
    """The flagship. Perfect agreement over a window twice the stated minimum,
    a perfect SLO block on every observation, and one widened axis.

    Four assertions, and each is a separate half of issue #1222:
      * the decision is a refusal, named to the axis that moved;
      * NO tally was built, so the accumulated agreement was not consulted;
      * the measured stages are recorded as not reached, so the artifact says
        so rather than leaving a reader to infer it;
      * the SLO blocks were read zero times, which is counted on the
        observations themselves and not asserted by the module about itself.
    """
    diff = dict(EMPTY_DIFF)
    diff[axis] = ["net:outbound"]
    obs = window(16)
    verdict = sp.decide(plan(authority_diff=diff), obs, key=KEY,
                        verifier=stub_verifier)

    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.AUTHORITY_WIDENED
    assert axis in verdict.refusal.reason

    assert verdict.tally is None
    assert verdict.measurements_read is False
    stages = {entry["stage"]: entry["status"] for entry in
              verdict.preconditions}
    assert stages["authority"] == "refused"
    assert stages["state"] == "not reached"

    assert verdict.slo_supplied == 16
    assert verdict.slo_reads == 0
    assert sum(o.slo_reads for o in obs) == 0


@pytest.mark.parametrize("axis", sp.AUTHORITY_AXES)
def test_an_unmeasured_axis_refuses_rather_than_reading_as_empty(axis):
    diff = {a: [] for a in sp.AUTHORITY_AXES if a != axis}
    verdict = decide(plan(authority_diff=diff))
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.DIFF_UNMEASURED
    assert axis in verdict.refusal.reason
    assert verdict.tally is None


def test_an_authority_axis_that_is_not_a_sequence_refuses():
    diff = dict(EMPTY_DIFF)
    diff["capabilities"] = True
    verdict = decide(plan(authority_diff=diff))
    assert verdict.refusal.link == sp.DIFF_UNMEASURED


def test_the_widened_and_unmeasured_cases_are_different_codes():
    """PR #1241's point: a reader needs to know WHICH authority moved, and an
    axis nobody measured is a different failure from an axis that widened."""
    assert sp.AUTHORITY_WIDENED != sp.DIFF_UNMEASURED


def test_the_authority_barrier_survives_a_window_that_would_have_promoted():
    """The same window, run twice. The only difference is one non-empty axis,
    and it is the difference between PROMOTE and a refusal."""
    obs_a, obs_b = window(16), window(16)
    good = sp.decide(plan(), obs_a, key=KEY, verifier=stub_verifier)
    diff = dict(EMPTY_DIFF, capabilities=["fs:/etc"])
    bad = sp.decide(plan(authority_diff=diff), obs_b, key=KEY,
                    verifier=stub_verifier)
    assert good.decision == sp.PROMOTE and good.tally.agreement == 1.0
    assert bad.decision == sp.REFUSE and bad.tally is None


# --------------------------------------------------------------------------
# 3. the barrier is structural: an AST walk over the module
# --------------------------------------------------------------------------

def module_ast():
    return ast.parse(MODULE_PATH.read_text(encoding="utf-8"),
                     feature_version=(3, 11))


def function_named(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined in the module")


ANSWER_READS = ("incumbent_answer", "candidate_answer", "_incumbent",
                "_candidate", "chosen", "candidates", "outcome")


@pytest.mark.parametrize("name", [
    "_check_plan", "_precondition_route", "_precondition_evidence",
    "_precondition_policy", "_precondition_authority", "_precondition_state",
])
def test_no_precondition_can_read_what_the_model_answered(name):
    """The data half of the barrier. `Pair` keeps the answer members private
    and a precondition is handed nothing else, so a precondition cannot
    compute agreement even by mistake. This asserts it on the source rather
    than on a comment."""
    node = function_named(module_ast(), name)
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute):
            assert child.attr not in ANSWER_READS, \
                f"{name} reads {child.attr}, which is an answer member"
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            assert child.value not in sp.AGREEMENT_MEMBERS, \
                f"{name} names the agreement member {child.value!r}"


def test_admit_does_not_carry_a_metric_across_the_barrier():
    """`Pair` has no SLO member at all, so the metric evidence is structurally
    absent downstream rather than merely unread."""
    fields = sp.Pair.__dataclass_fields__
    for member in sp.SLO_MEMBERS + ("slo",):
        assert member not in fields
    node = function_named(module_ast(), "admit")
    for child in ast.walk(node):
        assert not (isinstance(child, ast.Attribute) and child.attr == "slo")


def test_only_the_observation_property_reads_slo():
    """One `.slo` attribute read in the whole module, and it is the tripwire
    property's own return. Anything else would mean a metric had found a way
    into the decision."""
    tree = module_ast()
    readers = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for child in ast.walk(node):
                if isinstance(child, ast.Attribute) and child.attr in (
                        "slo", "_slo"):
                    readers.append(node.name)
    assert sorted(set(readers)) == ["__init__", "has_slo", "slo"]


def test_accumulate_is_called_after_the_precondition_walk_returns():
    """The control-flow half. `decide` walks PRECONDITIONS and returns inside
    the loop; the call to `accumulate` is textually and structurally after
    it, so no precondition's verdict can be raised by a measured value."""
    node = function_named(module_ast(), "decide")
    loops = {}
    for child in ast.walk(node):
        if isinstance(child, ast.For) and isinstance(child.iter, ast.Name) \
                and child.iter.id in ("PRECONDITIONS", "PLAN_PRECONDITIONS"):
            loops[child.iter.id] = child
    assert set(loops) == {"PRECONDITIONS", "PLAN_PRECONDITIONS"}
    for loop in loops.values():
        assert any(isinstance(n, ast.Return) for n in ast.walk(loop)), \
            "a precondition walk must RETURN on a refusal, not collect it"
    admits = [n for n in ast.walk(node)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "admit"]
    assert len(admits) == 1
    assert loops["PLAN_PRECONDITIONS"].end_lineno < admits[0].lineno, \
        "the route gate needs no evidence and is checked before admission"
    calls = [n for n in ast.walk(node)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "accumulate"]
    assert len(calls) == 1
    assert calls[0].lineno > loops["PRECONDITIONS"].end_lineno


def test_the_refusal_links_partition_into_precondition_and_measured():
    assert set(sp.PRECONDITION_LINKS) | set(sp.MEASURED_LINKS) == set(sp.LINKS)
    assert not set(sp.PRECONDITION_LINKS) & set(sp.MEASURED_LINKS)
    assert len(set(sp.LINKS)) == len(sp.LINKS)


def test_a_refusal_carrying_a_measured_link_always_has_a_tally():
    """The partition, as a property of real verdicts rather than of the two
    tuples. A measured link may be reported only when the barrier was passed,
    and a precondition link only when it was not."""
    cases = [
        decide(),
        decide(obs=[]),
        decide(plan(candidate_role="elsewhere")),
        decide(plan(authority_diff=dict(EMPTY_DIFF, origins=["secret"]))),
        decide(plan(layers={})),
        decide(obs=window(2)),
        decide(p=plan(threshold=1.0), obs=window(8, diverge_at=0)),
        decide(plan(live=True), window(8, diverge_at=2)),
    ]
    for verdict in cases:
        if verdict.refusal is None:
            assert verdict.decision == sp.PROMOTE
            continue
        if verdict.refusal.link in sp.MEASURED_LINKS:
            assert verdict.tally is not None, verdict.refusal.link
        else:
            assert verdict.tally is None, verdict.refusal.link


# --------------------------------------------------------------------------
# 4. the measured phase
# --------------------------------------------------------------------------

def test_a_window_below_the_stated_sample_refuses():
    verdict = decide(obs=window(3))
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.SAMPLE_TOO_SMALL
    assert verdict.tally.paired == 3


def test_agreement_below_the_threshold_refuses_and_names_the_first_divergence():
    verdict = decide(p=plan(threshold=1.0), obs=window(8, diverge_at=5))
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.AGREEMENT_BELOW_THRESHOLD
    assert verdict.refusal.where == ("Classifier", 5)
    assert "chosen_digest" in verdict.refusal.reason
    assert verdict.tally.agreement == pytest.approx(7 / 8)


def test_a_divergence_is_attributed_to_a_crossing_and_a_member():
    tally = decide(obs=window(6, diverge_at=2)).tally
    divergence = tally.first_divergence
    assert divergence.crossing == ("Classifier", 2)
    assert divergence.member == "chosen_digest"
    assert divergence.incumbent == DIGEST_A
    assert divergence.candidate == DIGEST_B
    assert "Classifier step 2" in divergence.describe()


def test_a_refused_outcome_diverges_and_is_attributed():
    tally = decide(obs=window(6, diverge_at=4, diverge_outcome=True)).tally
    divergence = tally.first_divergence
    assert divergence.crossing == ("Classifier", 4)
    assert divergence.member == "chosen_digest"
    assert divergence.candidate is None
    # and the outcome differs too, which the next member would have caught
    assert tally.diverged == 1


def test_a_below_threshold_window_that_still_meets_it_promotes():
    verdict = decide(p=plan(threshold=0.8), obs=window(10, diverge_at=7))
    assert verdict.decision == sp.PROMOTE
    assert verdict.tally.agreement == pytest.approx(0.9)


def test_the_model_digest_and_the_fallback_depth_are_not_compared():
    """A successor reaching the same answer with different weights, from a
    different rung of the ladder, at a different temperature, AGREES. That is
    the case a promotion exists to allow."""
    obs = []
    for step in range(5):
        obs.append(sp.Observation(
            record(step_index=step, role="local", residence="on_device",
                   placement=PLACEMENT_LOCAL, answer=DIGEST_A,
                   model_digest="4" * 64, fallback_depth=0),
            record(step_index=step, role="cloud", placement=PLACEMENT_CLOUD,
                   answer=DIGEST_A, model_digest="5" * 64, fallback_depth=2,
                   sampling={"max_tokens": 9, "seed": None,
                             "stop_digest": None, "temperature": 0.7,
                             "top_k": 40, "top_p": 0.9})))
    verdict = decide(obs=obs)
    assert verdict.decision == sp.PROMOTE
    assert verdict.tally.agreement == 1.0


# --------------------------------------------------------------------------
# 5. issue #1225: restore is not compensate
# --------------------------------------------------------------------------

def test_a_live_promotion_reverts_on_the_first_attributed_divergence():
    verdict = decide(p=plan(live=True, threshold=0.5),
                     obs=window(20, diverge_at=1))
    assert verdict.decision == sp.REVERT
    assert verdict.refusal.link == sp.DIVERGENCE_ATTRIBUTED
    assert verdict.refusal.where == ("Classifier", 1)
    assert verdict.tally.agreement == pytest.approx(19 / 20), \
        "the ratio is well above the threshold and the revert stands anyway"


def test_the_same_window_before_promotion_is_judged_by_the_threshold():
    """The two rules, side by side. Not live: the ratio decides. Live: the
    first attributed divergence decides."""
    obs_a, obs_b = window(20, diverge_at=1), window(20, diverge_at=1)
    before = decide(p=plan(live=False, threshold=0.5), obs=obs_a)
    after = decide(p=plan(live=True, threshold=0.5), obs=obs_b)
    assert before.decision == sp.PROMOTE
    assert after.decision == sp.REVERT


def test_a_revert_reports_restored_and_compensated_separately():
    verdict = decide(p=plan(live=True), obs=window(8, diverge_at=0))
    restoration = verdict.restoration
    assert restoration["restored"] == ["route-arm"]
    assert [entry["layer"] for entry in restoration["compensated"]] == \
        ["agreement-ledger", "placement-history"]
    assert restoration["neither"] == []
    assert restoration["fully_restored"] is False


def test_a_fully_revertible_promotion_reports_a_full_restore():
    layers = {layer: "revertible" for layer in sp.PROMOTION_LAYERS}
    verdict = decide(p=plan(live=True, layers=layers, compensations={}),
                     obs=window(8, diverge_at=0))
    assert verdict.decision == sp.REVERT
    assert verdict.restoration["fully_restored"] is True
    assert verdict.restoration["compensated"] == []


def test_the_report_never_calls_both_outcomes_rolled_back():
    text = sp.render(decide(p=plan(live=True), obs=window(8, diverge_at=0)))
    assert "rolled back" not in text.lower()
    assert "restored: route-arm" in text
    assert "compensated only: agreement-ledger" in text
    assert "compensated only: placement-history" in text


def test_a_layer_with_no_declared_state_class_refuses():
    layers = dict(REVERTIBLE_LAYERS)
    del layers["placement-history"]
    verdict = decide(plan(layers=layers))
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.LAYER_UNDECLARED
    assert "placement-history" in verdict.refusal.reason


def test_a_layer_outside_the_state_vocabulary_refuses():
    layers = dict(REVERTIBLE_LAYERS, **{"route-arm": "probably"})
    verdict = decide(plan(layers=layers))
    assert verdict.refusal.link == sp.PLAN_VOCABULARY


def test_a_neither_layer_with_no_compensation_may_not_be_promoted():
    """Issue #1225's second exit bullet, verbatim."""
    verdict = decide(plan(compensations={
        "agreement-ledger": {"name": "supersede-window", "tested": True}}))
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.STATE_NOT_RESTORABLE
    assert "placement-history" in verdict.refusal.reason
    assert verdict.tally is None


def test_an_untested_compensation_is_a_claim_not_evidence():
    compensations = {k: dict(v) for k, v in COMPENSATIONS.items()}
    compensations["placement-history"]["tested"] = False
    verdict = decide(plan(compensations=compensations))
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.COMPENSATION_UNTESTED


def test_the_state_precondition_runs_before_any_measurement():
    obs = window(16)
    verdict = sp.decide(plan(compensations={}), obs, key=KEY,
                        verifier=stub_verifier)
    assert verdict.refusal.link == sp.STATE_NOT_RESTORABLE
    assert verdict.tally is None
    assert sum(o.slo_reads for o in obs) == 0


# --------------------------------------------------------------------------
# 6. the plan itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize("threshold", [0.0, -0.1, 1.5, "0.9", True])
def test_a_threshold_outside_zero_to_one_is_not_a_stated_threshold(threshold):
    verdict = decide(plan(threshold=threshold))
    assert verdict.refusal.link == sp.PLAN_MALFORMED


@pytest.mark.parametrize("minimum", [0, -1, 1.5, "4", True])
def test_a_non_positive_minimum_sample_refuses(minimum):
    verdict = decide(plan(min_observations=minimum))
    assert verdict.refusal.link == sp.PLAN_MALFORMED


def test_a_plan_that_is_not_a_plan_refuses():
    verdict = sp.decide({"component": "Classifier"}, window(), key=KEY,
                        verifier=stub_verifier)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.PLAN_MALFORMED


def test_every_precondition_link_is_reachable():
    """Non-vacuity of the link table. Every named precondition refusal is
    produced by a real call in this file, so none of them is a code that can
    never fire."""
    produced = set()
    for verdict in (
            sp.decide({"x": 1}, window(), key=KEY, verifier=stub_verifier),
            decide(plan(threshold=2.0)),
            decide(plan(layers=dict(REVERTIBLE_LAYERS, **{"route-arm": "x"}))),
            decide(plan(action="nope")),
            decide(plan(candidate_role="elsewhere")),
            decide(plan(candidate_role="local")),
            decide(obs=[]),
            decide(obs=[sp.Observation(
                dict(record(step_index=0, role="local",
                            residence="on_device",
                            placement=PLACEMENT_LOCAL), signature="nope"),
                record(step_index=0, role="cloud",
                       placement=PLACEMENT_CLOUD))]),
            sp.decide(plan(), window(), key=None, verifier=stub_verifier),
            decide(obs=[observation(0), observation(0)]),
            decide(obs=[sp.Observation(
                record(step_index=0, role="cloud", placement=PLACEMENT_CLOUD),
                record(step_index=0, role="local", residence="on_device",
                       placement=PLACEMENT_LOCAL))]),
            decide(obs=[sp.Observation(
                record(step_index=0, role="local", residence="on_device",
                       placement=PLACEMENT_LOCAL, binding_mode="suppressed",
                       binding_value=None),
                record(step_index=0, role="cloud",
                       placement=PLACEMENT_CLOUD,
                       binding_mode="suppressed", binding_value=None))]),
            decide(obs=[sp.Observation(
                record(step_index=0, role="local", residence="on_device",
                       placement=PLACEMENT_LOCAL, binding_value=DIGEST_A),
                record(step_index=0, role="cloud",
                       placement=PLACEMENT_CLOUD, binding_value=DIGEST_B))]),
            decide(obs=[observation(i, policy=None) for i in range(5)]),
            decide(obs=[observation(i, placement=None) for i in range(5)]),
            decide(plan(authority_diff={})),
            decide(plan(authority_diff=dict(EMPTY_DIFF, budget=["cpu"]))),
            decide(plan(layers={})),
            decide(plan(compensations={})),
            decide(plan(compensations={
                layer: {"name": "x", "tested": False}
                for layer in sp.PROMOTION_LAYERS})),
    ):
        if verdict.refusal is not None:
            produced.add(verdict.refusal.link)
    missing = set(sp.PRECONDITION_LINKS) - produced - {
        sp.EVIDENCE_UNVERIFIABLE, sp.CROSSING_MISMATCHED}
    assert not missing, sorted(missing)
    assert sp.CROSSING_MISMATCHED in produced
    assert sp.EVIDENCE_UNVERIFIABLE in produced


def test_every_measured_link_is_reachable():
    produced = set()
    for verdict in (
            decide(obs=window(2)),
            decide(p=plan(threshold=1.0), obs=window(8, diverge_at=3)),
            decide(p=plan(live=True), obs=window(8, diverge_at=3)),
    ):
        produced.add(verdict.refusal.link)
    assert produced == set(sp.MEASURED_LINKS)


def test_render_states_that_the_measurement_was_not_read():
    text = sp.render(decide(plan(authority_diff=dict(EMPTY_DIFF,
                                                     origins=["secret"]))))
    assert "authority: refused" in text
    assert "state: not reached" in text
    assert "measured evidence: not read" in text
    assert "read: 0" in text


def test_the_verdict_round_trips_to_json():
    payload = json.dumps(decide().as_dict(), sort_keys=True)
    back = json.loads(payload)
    assert back["decision"] == sp.PROMOTE
    assert back["measurements_read"] is True
    assert back["slo_reads"] == 0
    assert back["kind"] == sp.PROMOTION_KIND


# --------------------------------------------------------------------------
# 7. item 517 integration: the records are ITS records, not a parallel log
# --------------------------------------------------------------------------

try:  # noqa: SIM105 - item 517 (PR #1239) may not be on this tree yet
    from revl import model_evidence as _model_evidence
except Exception:  # noqa: BLE001
    _model_evidence = None

needs_517 = pytest.mark.skipif(
    _model_evidence is None,
    reason="item 517 (revl.model_evidence) is not on this tree yet")


@needs_517
def test_the_crossing_key_is_item_517s_own():
    """No second correlation. `(component, step_index)` is
    `revl.wal.model_decisions`' key and `model_evidence.crossing_key` reads
    it; this module must agree with that function and not with a copy."""
    sample = record(step_index=11)
    assert sp._crossing(sample) == _model_evidence.crossing_key(sample)
    assert sp._crossing(sample) == ("Classifier", 11)


@needs_517
def test_a_window_of_really_sealed_records_promotes():
    """End to end against item 517's own `seal` and `verify`."""
    key = b"a real key for the real verifier"
    obs = []
    for step in range(6):
        common = dict(
            component="Classifier", step_index=step,
            model_digest=DIGEST_B, placement_digest=PLACEMENT_LOCAL,
            prompt_binding={"mode": "content-addressed", "value": DIGEST_C,
                            "reason": None},
            origins=["input"], candidates=[DIGEST_A], chosen=0,
            outcome="validated",
            sampling={"max_tokens": 256, "seed": 7, "stop_digest": None,
                      "temperature": 0.0, "top_k": None, "top_p": 1.0},
            policy_digest=POLICY, fallback_depth=0)
        incumbent = _model_evidence.seal(
            key, role="local", residence="on_device", **common)
        candidate = _model_evidence.seal(
            key, role="cloud", residence="off_device",
            **dict(common, placement_digest=PLACEMENT_CLOUD))
        obs.append(sp.Observation(incumbent, candidate, slo=PERFECT_SLO))
    verdict = sp.decide(plan(min_observations=4), obs, key=key)
    assert verdict.decision == sp.PROMOTE, verdict.refusal
    assert verdict.slo_reads == 0


@needs_517
def test_a_really_sealed_record_that_was_edited_refuses():
    key = b"a real key for the real verifier"
    obs = []
    for step in range(6):
        common = dict(
            component="Classifier", step_index=step,
            model_digest=DIGEST_B, placement_digest=PLACEMENT_LOCAL,
            prompt_binding={"mode": "content-addressed", "value": DIGEST_C,
                            "reason": None},
            origins=["input"], candidates=[DIGEST_A], chosen=0,
            outcome="validated",
            sampling={"max_tokens": 256, "seed": 7, "stop_digest": None,
                      "temperature": 0.0, "top_k": None, "top_p": 1.0},
            policy_digest=POLICY, fallback_depth=0)
        incumbent = _model_evidence.seal(
            key, role="local", residence="on_device", **common)
        candidate = _model_evidence.seal(
            key, role="cloud", residence="off_device",
            **dict(common, placement_digest=PLACEMENT_CLOUD))
        obs.append(sp.Observation(incumbent, candidate, slo=PERFECT_SLO))
    obs[3].candidate["candidates"] = [DIGEST_B]
    verdict = sp.decide(plan(min_observations=4), obs, key=key)
    assert verdict.decision == sp.REFUSE
    assert verdict.refusal.link == sp.EVIDENCE_UNVERIFIED


@needs_517
def test_the_residence_vocabularies_agree():
    assert set(_model_evidence.RESIDENCES) == set(model_route.RESIDENCES)

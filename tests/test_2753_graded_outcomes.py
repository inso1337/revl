"""Graded evidence outcomes for `policy evaluate` (issue #2753).

The evidence rule family is a HARD predicate: every clause in a
`requires evidence` rule must hold or the gate refuses. This slice adds a
SEPARATE, advisory grading layer over the same evaluated clauses:

  * `component C review on evidence [...]` marks a soft rule. Its failing
    clauses never add a violation and never change the gate; they map the
    candidate to the `review` outcome instead of an admission or a refusal.
  * `observe`, `canary` and `review` are the operator-declared soft outcomes.
    `admit` is computed (every hard clause holds, no soft clause fails) and
    `quarantine`/`refuse` are computed from a hard failure, never declared.
  * A threshold remains the operator's: the soft rule names its own facet and
    threshold, and no default is baked in.
  * The numeric-confidence parse error keeps firing on both forms.

These tests pin the acceptance criteria from the issue.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import registry as reg  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.policy import (  # noqa: E402
    PolicyError, evaluate, explain, parse_policy, render_explain,
)

SOLO = """
service Store { emission[db] fn put(key: Str, value: Str) }
component CsvReader requires store: Store {
  emit store.put("a", "b")
}
"""

TWO = """
service Store { emission[db] fn put(key: Str, value: Str) }
component CsvReader requires store: Store {
  emit store.put("a", "b")
}
component LocalMain requires store: Store {
  emit store.put("c", "d")
}
"""

IDEMPOTENT_SOLO = """
service Store { emission[db] idempotent fn put(key: Str, value: Str) }
component CsvReader requires store: Store {
  emit store.put("a", "b")
}
"""

WITNESSED = """
type Stash = { path: Str }
type FsError = { code: Str }
extern pure fn unstash(w: Stash) -> Unit = @py { pass }
extern witnessed[fs] fn stash() -> Result[Stash, FsError] undo unstash(result) = @py { pass }
component C { effect stash() }
"""


def _audit(src=SOLO):
    return audit_report(compile_source(src))


def _bundle(*, sweep=(8, 12), gauntlet=None, roundtrip=None):
    bundle = reg.EvidenceBundle(
        fault_sweep={"kind": "revl.fault-sweep", "status": "passed",
                     "counts": {"steps": sweep[1], "passed": sweep[0],
                                "unreachable": 0}},
        capabilities={"kind": "revl.capabilities", "boundary": {}})
    if gauntlet is not None:
        bundle.gauntlet = gauntlet
    if roundtrip is not None:
        bundle.inverse_roundtrip = roundtrip
    return bundle


def _explain(policy_text, *, evidence=None):
    return explain(parse_policy(policy_text), _audit(),
                   evidence=evidence or {},
                   origins={"CsvReader": "source"})


HARD_PASS = "component CsvReader requires evidence [capabilities present]"


# --------------------------------------- a soft failure grades `review`

def test_soft_clause_failure_yields_review_naming_the_clause():
    """The issue's first acceptance criterion: a candidate that fails one soft
    clause gets `review` with that clause named, not only a violation list."""
    policy_text = (HARD_PASS + "\n"
                   "component CsvReader review on evidence "
                   "[fault-sweep full] self-attested\n")
    bundle = _bundle(sweep=(8, 12))
    result = _explain(policy_text, evidence={"CsvReader": bundle})

    # the graded outcome is review, per component and overall.
    assert result["outcome"]["name"] == "review"
    assert result["components"][0]["outcome"]["name"] == "review"

    # it carries the clause that decided it, with the recorded fact.
    decided = result["outcome"]["decidedBy"]
    assert decided["component"] == "CsvReader"
    assert decided["facet"] == "fault-sweep"
    assert decided["threshold"] == "full"
    assert "8/12" in decided["fact"]
    assert decided["rule"] == ("component CsvReader review on evidence "
                               "[fault-sweep full] self-attested")

    # and the gate is untouched: a soft failure is not a violation.
    assert result["refused"] is False
    assert evaluate(parse_policy(policy_text), _audit(),
                    evidence={"CsvReader": bundle},
                    origins={"CsvReader": "source"}) == []


def test_all_clauses_holding_grades_admit():
    policy_text = (HARD_PASS + "\n"
                   "component CsvReader canary on evidence "
                   "[capabilities present]\n")
    result = _explain(policy_text, evidence={"CsvReader": _bundle()})
    assert result["outcome"]["name"] == "admit"
    assert result["outcome"]["hard"] is False
    assert result["outcome"]["decidedBy"] is None
    assert result["refused"] is False


def test_soft_rule_is_marked_in_the_human_report():
    text = render_explain(_explain(
        "component CsvReader review on evidence [fault-sweep full] self-attested",
        evidence={"CsvReader": _bundle(sweep=(8, 12))}))
    assert "graded outcome: review" in text
    assert "fault-sweep" in text


def test_inert_soft_selector_is_reported_as_grading_nothing():
    result = _explain("component Nope* review on evidence "
                      "[fault-sweep full] self-attested")
    assert result["inertSelectors"]
    assert "review on evidence" in result["inertSelectors"][0]["rule"]
    assert "grades nothing" in render_explain(result)


# --------------------------------------- a hard failure never admits

@pytest.mark.parametrize("declared, expected", [
    ("", "refuse"),
    ("quarantine required\n", "quarantine"),
])
def test_hard_failure_never_returns_admit(declared, expected):
    """The issue's second acceptance criterion, in both operator settings: a
    failing hard clause keeps the gate refusal and never grades `admit`. The
    policy's `quarantine required` line chooses the quarantine recommendation
    instead of a plain refusal; without it the outcome is the gate's own
    `refuse`."""
    policy_text = declared + (
        "component CsvReader requires evidence [fault-sweep full] self-attested")
    result = _explain(policy_text, evidence={"CsvReader": _bundle(sweep=(8, 12))})
    assert result["outcome"]["name"] == expected
    assert result["outcome"]["name"] != "admit"
    assert result["outcome"]["hard"] is True
    assert result["refused"] is True


def test_hard_and_soft_failure_still_refuses_and_names_the_hard_clause():
    policy_text = (
        "component CsvReader requires evidence [capabilities present]\n"
        "component CsvReader requires evidence [attestation valid]\n"
        "component CsvReader review on evidence [fault-sweep full] self-attested\n")
    result = _explain(policy_text, evidence={"CsvReader": _bundle(sweep=(8, 12))})
    # both the hard attestation clause and the soft sweep clause fail; the hard
    # failure decides, so the graded answer cannot read as a soft review.
    assert result["outcome"]["name"] == "refuse"
    assert result["outcome"]["hard"] is True
    assert result["outcome"]["decidedBy"]["facet"] == "attestation"
    assert result["refused"] is True


def test_register_floor_failure_never_grades_admit():
    """A `requires register` floor is hard and in the same admission family, so
    the outcome must not say `admit` while the gate refuses on it."""
    result = explain(parse_policy("capability db requires register declared"),
                     _audit(), origins={"CsvReader": "source"})
    assert result["outcome"]["name"] == "refuse"
    assert result["outcome"]["hard"] is True
    decided = result["outcome"]["decidedBy"]
    assert decided["facet"] == "register:db"
    assert decided["rule"] == "capability db requires register declared"
    assert result["refused"] is True


def test_register_floor_holding_grades_admit():
    result = explain(parse_policy("capability db requires register declared"),
                     _audit(IDEMPOTENT_SOLO),
                     origins={"CsvReader": "source"})
    assert result["outcome"]["name"] == "admit"
    assert result["refused"] is False


def test_reach_deny_forces_a_hard_refuse_outcome():
    """A refusal from another rule family must not sit beside `admit`: the
    graded outcome becomes `refuse` and names the violation."""
    result = _explain(
        "component CsvReader may not reach db\n"
        "component CsvReader requires evidence [capabilities present]",
        evidence={"CsvReader": _bundle()})
    comp = result["components"][0]
    assert comp["refused"] is True
    assert comp["outcome"]["name"] == "refuse"
    assert comp["outcome"]["hard"] is True
    assert comp["outcome"]["decidedBy"]["kind"] == "deny"
    assert comp["outcome"]["decidedBy"]["token"] == "db"
    assert result["outcome"]["name"] == "refuse"
    assert result["outcome"]["hard"] is True
    assert "graded outcome: admit" not in render_explain(result)


def test_teardown_refusal_forces_a_hard_refuse_outcome():
    """The teardown floor is document-global and its violation names the
    recovery entry, not a component. The overall outcome must still refuse;
    the owning component's evidence grade stays whatever its clauses say."""
    policy = parse_policy(
        "requires idempotent-teardown\n"
        "component C requires evidence [capabilities present]")
    result = explain(policy, _audit(WITNESSED),
                     evidence={"C": _bundle()}, origins={"C": "source"})
    assert result["refused"] is True
    assert result["outcome"]["name"] == "refuse"
    assert result["outcome"]["hard"] is True
    assert result["outcome"]["decidedBy"]["kind"] == "teardown"
    assert result["outcome"]["decidedBy"]["token"] == "stash"


# --------------------------------------- soft outcome precedence

def test_strictest_failing_soft_outcome_decides():
    policy_text = (
        "component CsvReader observe on evidence "
        "[fault-sweep full] self-attested\n"
        "component CsvReader canary on evidence "
        "[gauntlet admissible] self-attested\n"
        "component CsvReader review on evidence "
        "[inverse-roundtrip pass] self-attested\n")
    result = _explain(policy_text, evidence={"CsvReader": _bundle(sweep=(8, 12))})
    # all three soft clauses fail: review is stricter than canary and observe.
    assert result["outcome"]["name"] == "review"
    assert result["outcome"]["decidedBy"]["facet"] == "inverse-roundtrip"
    assert result["refused"] is False


def test_observe_is_the_lightest_soft_outcome():
    result = _explain(
        "component CsvReader observe on evidence "
        "[fault-sweep full] self-attested",
        evidence={"CsvReader": _bundle(sweep=(8, 12))})
    assert result["outcome"]["name"] == "observe"


def test_quarantine_requires_a_hard_failure():
    """`quarantine required` chooses the disposition of a HARD failure only;
    a soft failure still grades its declared outcome and leaves the gate
    clean."""
    result = _explain(
        "quarantine required\n"
        "component CsvReader observe on evidence "
        "[fault-sweep full] self-attested",
        evidence={"CsvReader": _bundle(sweep=(8, 12))})
    assert result["outcome"]["name"] == "observe"
    assert result["outcome"]["hard"] is False
    assert result["refused"] is False


def test_passing_soft_clause_does_not_mask_a_hard_failure():
    policy_text = (
        "component CsvReader requires evidence [fault-sweep full] self-attested\n"
        "component CsvReader review on evidence [capabilities present]\n")
    result = _explain(policy_text, evidence={"CsvReader": _bundle(sweep=(8, 12))})
    assert result["outcome"]["name"] == "refuse"
    assert result["outcome"]["decidedBy"]["facet"] == "fault-sweep"
    assert result["refused"] is True


def test_overall_outcome_is_the_whole_composition_when_narrowed():
    """`--component` narrows the reported components, not the overall outcome:
    the worst outcome across the whole composition still decides, so a narrow
    clean report cannot hide a failing sibling."""
    policy = parse_policy(
        "component CsvReader review on evidence "
        "[fault-sweep full] self-attested\n"
        "component LocalMain canary on evidence "
        "[fault-sweep full] self-attested")
    result = explain(
        policy, _audit(TWO),
        evidence={"CsvReader": _bundle(sweep=(8, 12)),
                  "LocalMain": _bundle(sweep=(8, 12))},
        origins={"CsvReader": "source", "LocalMain": "source"},
        component="LocalMain")
    assert [c["component"] for c in result["components"]] == ["LocalMain"]
    assert result["components"][0]["outcome"]["name"] == "canary"
    assert result["outcome"]["name"] == "review"
    assert result["outcome"]["decidedBy"]["component"] == "CsvReader"


# --------------------------------------- numeric confidence stays rejected

@pytest.mark.parametrize("rule", [
    "component CsvReader requires evidence [confidence 0.9]",
    "component CsvReader review on evidence [confidence 0.9]",
    "component CsvReader requires evidence [fault-sweep 0.9]",
])
def test_numeric_confidence_is_still_a_parse_error(rule):
    """The issue's third acceptance criterion: confidence can only enter as an
    objective facet, so a numeric confidence (or numeric threshold) is still a
    PolicyError, including on the new soft form."""
    with pytest.raises(PolicyError) as exc:
        parse_policy(rule + " self-attested")
    assert "numeric confidence" in str(exc.value)


# --------------------------------------- the soft outcome is the operator's

def test_soft_outcome_is_not_declared_as_admit_or_quarantine():
    """`admit` and `quarantine` are computed from the hard predicate, so the
    grammar refuses to let an operator declare them as soft outcomes."""
    for outcome in ("admit", "quarantine"):
        with pytest.raises(PolicyError) as exc:
            parse_policy(f"component CsvReader {outcome} on evidence "
                         f"[fault-sweep full] self-attested")
        assert "computed" in str(exc.value)


def test_unknown_soft_outcome_is_a_parse_error():
    with pytest.raises(PolicyError) as exc:
        parse_policy("component CsvReader reviw on evidence "
                     "[fault-sweep full] self-attested")
    assert "review" in str(exc.value)


# --------------------------------------- JSON form parity

def test_mixed_case_soft_rule_parses():
    policy = parse_policy("component CsvReader REVIEW ON EVIDENCE "
                          "[fault-sweep full] SELF-ATTESTED")
    assert policy.evidence_rules[0].outcome == "review"
    assert policy.evidence_rules[0].require == (("fault-sweep", "full"),)


def test_documented_soft_example_parses():
    """The commands-reference snippet must be copy-pasteable; a self-attesting
    facet on a soft rule still needs its unrooted acknowledgment. Read the
    actual fenced block, not a copy, so the doc cannot regress silently."""
    doc = (ROOT / "docs/commands-reference.md").read_text(encoding="utf-8")
    marker = "component vendored-* requires evidence [attestation valid]"
    at = doc.index(marker)
    fence_start = doc.rfind("```", 0, at)
    fence_end = doc.index("```", at)
    policy = parse_policy(doc[fence_start + 3:fence_end].strip("\n"))
    assert [r.outcome for r in policy.evidence_rules] == [
        None, "review", "canary", "observe"]


def test_json_policy_declares_a_soft_outcome():
    doc = {"evidence": [{
        "component": "CsvReader",
        "outcome": "review",
        "selfAttested": True,
        "require": {"fault-sweep": "full"},
    }]}
    result = explain(parse_policy(json.dumps(doc)), _audit(),
                     evidence={"CsvReader": _bundle(sweep=(8, 12))},
                     origins={"CsvReader": "source"})
    assert result["outcome"]["name"] == "review"
    assert result["outcome"]["decidedBy"]["facet"] == "fault-sweep"


def test_json_policy_rejects_a_computed_outcome():
    doc = {"evidence": [{
        "component": "CsvReader",
        "outcome": "quarantine",
        "require": {"fault-sweep": "full"},
    }]}
    with pytest.raises(PolicyError) as exc:
        parse_policy(json.dumps(doc))
    assert "computed" in str(exc.value)


# --------------------------------------- additivity

def test_evidence_free_policy_has_no_outcome_section():
    result = explain(parse_policy("component * may reach db"), _audit())
    assert "outcome" not in result
    assert all("outcome" not in comp for comp in result["components"])


def test_soft_rules_are_not_refusal_predictions():
    from revl.policy import predict_refusals, unpredicted_rules
    policy = parse_policy("component CsvReader review on evidence "
                          "[fault-sweep full] self-attested")
    assert unpredicted_rules(policy) == []
    prediction = predict_refusals(policy, "CsvReader")
    assert prediction["wouldBeRefused"] == []


def test_mixed_hard_and_soft_rules_only_predict_the_hard_refusal():
    from revl.policy import predict_refusals
    policy = parse_policy(
        "component CsvReader requires evidence [capabilities present]\n"
        "component CsvReader review on evidence "
        "[fault-sweep full] self-attested")
    prediction = predict_refusals(policy, "CsvReader")
    facets = [entry["facet"] for entry in prediction["wouldBeRefused"]]
    assert facets == ["capabilities"]


def test_policy_diff_leg_ignores_soft_rules():
    """A soft rule never refuses, so it must not move the `simulate
    policy-diff` evidence leg (adding one is not a widening, and changing its
    outcome is not a gate change)."""
    from revl.policy_diff import leg_surfaces
    hard = parse_policy(
        "component CsvReader requires evidence [capabilities present]")
    with_soft = parse_policy(
        "component CsvReader requires evidence [capabilities present]\n"
        "component CsvReader review on evidence "
        "[fault-sweep full] self-attested\n"
        "component CsvReader canary on evidence "
        "[gauntlet admissible] self-attested")
    assert (leg_surfaces(with_soft, "CsvReader", "db", frozenset())["evidence"]
            == leg_surfaces(hard, "CsvReader", "db", frozenset())["evidence"])
    soft_only = parse_policy(
        "component CsvReader review on evidence "
        "[fault-sweep full] self-attested")
    assert leg_surfaces(
        soft_only, "CsvReader", "db", frozenset())["evidence"] == ()


# --------------------------------------- CLI end to end

def test_cli_policy_evaluate_json_carries_the_graded_outcome(tmp_path, capsys):
    from revl.__main__ import main

    policy = tmp_path / "graded.policy"
    policy.write_text(
        "component CsvReader review on evidence [fault-sweep full] self-attested\n",
        encoding="utf-8")
    source = tmp_path / "app.rvl"
    source.write_text(SOLO, encoding="utf-8")

    code = main(["policy", "evaluate", str(policy), str(source), "--json"])
    out = json.loads(capsys.readouterr().out)
    # a soft failure is a graded outcome, not a gate refusal: exit stays 0.
    assert code == 0
    assert out["refused"] is False
    assert out["outcome"]["name"] == "review"
    assert out["outcome"]["decidedBy"]["facet"] == "fault-sweep"

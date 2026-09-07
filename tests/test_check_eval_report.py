"""Guard for the report-honesty checker (issue #478).

`tools/check_eval_report.py` reads one eval report and decides whether it obeys
the frozen eval-honesty protocol before its numbers or claims reach a public
surface. This test does NOT pin any compile-rate number; it asserts the checker
accepts an honest report and rejects each way a report could over-claim:

  - a self-scored report (the generator graded itself),
  - a brief that names no hard gate, or one outside the frozen set,
  - a claim standing higher than the rung its evidence justifies,
  - a bare 'claimed' claim marked public.

It also pins the four-rung ladder: measured / demonstrated / supported each need
strictly more evidence than the rung below, and independence (noSelfScore) is
required at every promotion.
"""

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import pytest  # noqa: E402

import check_eval_report as cer  # noqa: E402


def _valid_report() -> dict:
    """A minimal honest report: compiler-graded, named gate, a measured claim."""
    return {
        "protocol": "EVAL-1",
        "report_schema": cer.REPORT_SCHEMA,
        "compiler_commit": "abc1234",
        "metric": "passAtK=1",
        "grader": {"tool": "revl.compile_source", "kind": "compiler"},
        "generator": {"model": "deepseek-v4-pro", "run": "typed-deepseek-v4-pro"},
        "briefs": [
            {"spec": "auth-service", "variant": "v2",
             "hard_gate": "compiles", "result": "pass"},
        ],
        "claims": [
            {"text": "measured 24/30 first-pass on typed-deepseek-v4-pro at abc1234",
             "rung": "measured", "public": True,
             "evidence": {"compiler_commit": "abc1234",
                          "run": "typed-deepseek-v4-pro", "protocol": "EVAL-1"}},
        ],
    }


def test_valid_report_is_clean():
    assert cer.check_report(_valid_report()) == []


def test_rejects_self_scored_report():
    """A grader that is the generating model is refused (noSelfScore)."""
    report = _valid_report()
    report["grader"] = {"tool": "revl.compile_source", "kind": "compiler",
                        "model": "deepseek-v4-pro"}  # names the generator
    violations = cer.check_report(report)
    assert any("noSelfScore" in v for v in violations)


def test_rejects_grader_that_is_not_the_compiler():
    """A model-client grader is refused even if it is not the generator."""
    report = _valid_report()
    report["grader"] = {"tool": "openai.chat", "kind": "model"}
    violations = cer.check_report(report)
    assert any("noSelfScore" in v for v in violations)
    # And a measured claim cannot stand on a non-model-free report.
    assert any("ladder" in v for v in violations)


def test_rejects_brief_with_no_named_gate():
    report = _valid_report()
    del report["briefs"][0]["hard_gate"]
    violations = cer.check_report(report)
    assert any("named gate" in v for v in violations)


def test_rejects_brief_with_unknown_gate():
    report = _valid_report()
    report["briefs"][0]["hard_gate"] = "looksGoodToMe"
    violations = cer.check_report(report)
    assert any("named gate" in v and "frozen set" in v for v in violations)


def test_rejects_claim_above_its_evidenced_rung():
    """A 'supported' claim with only measured-level evidence is refused."""
    report = _valid_report()
    report["claims"][0]["rung"] = "supported"
    violations = cer.check_report(report)
    assert any("ladder" in v and "supported" in v for v in violations)


def test_rejects_public_claimed_floor():
    """The bare 'claimed' floor may never be public."""
    report = _valid_report()
    report["claims"][0] = {"text": "revl writes great code", "rung": "claimed",
                           "public": True}
    violations = cer.check_report(report)
    assert any("floor" in v for v in violations)


def test_claimed_floor_is_fine_when_not_public():
    report = _valid_report()
    report["claims"][0] = {"text": "hypothesis: v2host will beat v2",
                           "rung": "claimed", "public": False}
    assert cer.check_report(report) == []


def test_demonstrated_needs_an_independent_reproducer():
    report = _valid_report()
    claim = report["claims"][0]
    claim["rung"] = "demonstrated"
    # No reproduced_by yet: cannot stand above measured.
    assert any("demonstrated" in v for v in cer.check_report(report))
    # Reproduced by the generator itself: still not independent.
    claim["evidence"]["reproduced_by"] = "deepseek-v4-pro"
    assert any("demonstrated" in v for v in cer.check_report(report))
    # Reproduced by an independent party: now it stands.
    claim["evidence"]["reproduced_by"] = "human-reviewer-A"
    assert cer.check_report(report) == []


def test_supported_needs_an_independent_promotion_review():
    report = _valid_report()
    claim = report["claims"][0]
    claim["rung"] = "supported"
    claim["evidence"]["reproduced_by"] = "human-reviewer-A"
    # Demonstrated is reached, but no review yet.
    assert any("supported" in v for v in cer.check_report(report))
    # A review by the generator is not independent.
    claim["evidence"]["review"] = {"protocol": "EVAL-2", "reviewer": "deepseek-v4-pro"}
    assert any("supported" in v for v in cer.check_report(report))
    # An independent review promotes it.
    claim["evidence"]["review"] = {"protocol": "EVAL-2", "reviewer": "human-reviewer-B"}
    assert cer.check_report(report) == []


def test_lower_claims_are_always_allowed():
    """A report may claim a rung lower than its evidence would allow."""
    report = _valid_report()
    # Full supported-level evidence, but the report only claims measured.
    report["claims"][0]["evidence"].update(
        {"reproduced_by": "human-reviewer-A",
         "review": {"protocol": "EVAL-2", "reviewer": "human-reviewer-B"}})
    report["claims"][0]["rung"] = "measured"
    assert cer.check_report(report) == []


def test_malformed_report_raises_report_error():
    report = _valid_report()
    del report["grader"]
    with pytest.raises(cer.ReportError):
        cer.check_report(report)


def test_wrong_schema_version_is_malformed():
    report = _valid_report()
    report["report_schema"] = "EVAL-REPORT-99"
    with pytest.raises(cer.ReportError):
        cer.check_report(report)


def test_cli_exit_codes(tmp_path):
    """main() returns 0 for a clean report and 1 for a violating one."""
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_valid_report()))
    assert cer.main([str(good)]) == 0

    bad_report = _valid_report()
    bad_report["claims"][0]["rung"] = "supported"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(bad_report))
    assert cer.main([str(bad)]) == 1

    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps({"protocol": "EVAL-1"}))
    assert cer.main([str(malformed)]) == 2


def test_ladder_order_is_the_frozen_four_rungs():
    assert cer.LADDER == ("claimed", "measured", "demonstrated", "supported")

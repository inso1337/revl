"""`revl analyze` liveness: the IR->net derivation and its report (roadmap item
438). The bare Petri math is proved in ``tests/test_petri.py``; here we prove
the revl-specific half -- the derivation's arc-kind choices, the deadlock the
analyzer must catch (with a non-vacuity check that removing the consumable
classification makes it pass), the ambient-coeffect seeding that keeps every
real composition a NON-report, and the CLI dispatch."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.__main__ import main  # noqa: E402
from revl.liveness import analyze_document, derive  # noqa: E402


# ------------------------------------------------------- shared services are LIVE


def _shared_service_ir():
    """A provider and two consumers of an ORDINARY (shared) service -- the
    G3-clean multi-consumer shape that must NOT be flagged."""
    return {
        "manifest": {
            "components": [
                {"name": "Provider", "inject": [], "provides": ["db"]},
                {"name": "ConsumerA", "inject": ["db"], "provides": []},
                {"name": "ConsumerB", "inject": ["db"], "provides": []},
            ]
        }
    }


def test_shared_service_two_consumers_is_live():
    """A shared service is READ, not consumed, so two consumers coexist: no
    deadlock. This is the case a naive token model would wrongly flag."""
    report = analyze_document(_shared_service_ir(), name="shared")
    assert report.verdict == "live"
    assert report.findings == []
    assert report.exit_code() == 0


def test_shared_injects_use_read_arcs_not_consume():
    """The derivation must wire a shared inject as a READ arc so the provision
    place is not depleted -- the structural reason two consumers are fine."""
    d = derive(_shared_service_ir())
    ca = next(t for t in d.net.transitions if t.id == "act:ConsumerA")
    assert "prov:db@<shared>" in ca.read
    assert "prov:db@<shared>" not in ca.consume


def test_ambient_coeffect_is_seeded_not_a_deadlock():
    """A `requires` with no in-composition provider is a host-injected ambient
    coeffect (item 350), seeded available -- never a false-positive deadlock."""
    ir = {"manifest": {"components": [
        {"name": "Heartbeat", "inject": ["log"], "provides": []},
    ]}}
    report = analyze_document(ir, name="ambient")
    assert report.verdict == "live"


# --------------------------------------------- the deadlock the analyzer catches


def _consumable_contention_ir():
    """A provider of a SINGLE-CONSUMER coeffect (`ticks`, capacity 1) with two
    consumers -- the cross-component contention item 130's pointwise 3.1/3.6
    cannot see and G3 does not model (it is not a cycle). This is the future
    multicast-bridge shape (item 130 §4.1); the current frontend cannot yet emit
    it, which is why the corpus is all LIVE."""
    return {
        "manifest": {
            "components": [
                {"name": "Ticker", "inject": [], "provides": ["ticks"],
                 "consumable": ["ticks"]},
                {"name": "ConsumerA", "inject": ["ticks"], "provides": []},
                {"name": "ConsumerB", "inject": ["ticks"], "provides": []},
            ]
        }
    }


def test_consumable_contention_is_a_reported_deadlock():
    """Two consumers, one single-consumer token: whichever activates first
    strands the other. The analyzer reports BOTH stranded activations and names
    the rival that took the token."""
    report = analyze_document(_consumable_contention_ir(), name="deadlock")
    assert report.verdict == "deadlock"
    assert report.exit_code() == 1
    stranded = {f.component for f in report.findings}
    assert stranded == {"ConsumerA", "ConsumerB"}
    for f in report.findings:
        assert any("ticks" in w for w in f.waits_on)
        assert f.contended_with  # names the rival consumer


def test_non_vacuity_remove_the_check_and_it_passes():
    """Non-vacuity: the deadlock exists ONLY because `ticks` is classified
    consumable. Drop that classification (treat it as an ordinary shared
    service) and the SAME composition analyzes LIVE -- proving the report is the
    consumable-arc modeling doing real work, not an artifact."""
    ir = _consumable_contention_ir()
    assert analyze_document(ir, name="with-check").verdict == "deadlock"

    ir_no_check = json.loads(json.dumps(ir))
    for entry in ir_no_check["manifest"]["components"]:
        entry.pop("consumable", None)  # remove the check's input
    assert analyze_document(ir_no_check, name="no-check").verdict == "live"


def test_capacity_two_serves_two_consumers():
    """A consumable declared capacity 2 serves two consumers -- boundary check
    that contention is about scarcity, not merely being consumable."""
    ir = _consumable_contention_ir()
    ir["manifest"]["components"][0]["consumableCapacity"] = {"ticks": 2}
    assert analyze_document(ir, name="cap2").verdict == "live"


# ----------------------------------------------------------------- bounded / CLI


def test_bound_hit_reports_inconclusive_not_live():
    """A search truncated by the state cap must say INCONCLUSIVE and never claim
    liveness (item 418), even though no deadlock was seen within the bound."""
    report = analyze_document(_shared_service_ir(), name="capped", max_states=1)
    assert report.verdict == "inconclusive"
    assert report.bound_hit
    assert report.exit_code() == 0  # report-only: an unproven bound is not a refusal


def test_cli_analyze_live_source(capsys):
    """`revl analyze FILE.rvl` compiles and reports; a plain provider/consumer
    pair is live with exit 0."""
    rc = main(["analyze", str(ROOT / "examples" / "counter_pair.rvl")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "live" in out


def test_cli_analyze_ir_deadlock(capsys):
    """`revl analyze --ir DOC.json` on the deadlock fixture reports the cycle and
    exits nonzero (report-only nonzero, so CI can consume a PROVEN deadlock)."""
    fixture = ROOT / "tests" / "data" / "deadlock_consumable.ir.json"
    rc = main(["analyze", "--ir", str(fixture)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "DEADLOCK" in out
    assert "ConsumerA" in out and "ConsumerB" in out


def test_cli_analyze_stream_corpus_not_flagged(capsys):
    """The item-130 stream program (subscribe / merge / await) is legal and must
    NOT be flagged -- a regression guard on the zero-false-positive result."""
    rc = main(["analyze", str(ROOT / "backends" / "go" / "testdata" / "stream_130.rvl")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DEADLOCK" not in out

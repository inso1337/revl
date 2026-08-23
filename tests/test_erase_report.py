"""`revl erase-report --realm <r>` — the composed erasure artifact
(src/revl/erase_report.py, docs/erase-report.md, roadmap item 29).

Three claims, checked independently:

  * in-process state gone (the R4 no-residue proof over a real teardown),
  * every boundary crossing the realm's components make, compensated vs bare,
  * other realms provably untouched (the `survivors` set, EXACT).

The `erase_realms.rvl` fixture is two isolated realms `alpha` and `beta`
sharing a service type but never a provision; realm `alpha` makes one
compensated and one bare emission. The runtime no-residue proof needs the
cordis-py backend, so those assertions are guarded with `_has_runtime`.
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from revl.compiler import compile_files  # noqa: E402
from revl import erase_report  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REALMS = os.path.join(ROOT, "tests", "fixtures", "erase_realms.rvl")
TENANTS = os.path.join(ROOT, "examples", "tenants.rvl")


def _has_runtime() -> bool:
    try:
        import cordis  # noqa: F401,PLC0415
        return True
    except ModuleNotFoundError:
        return False


@pytest.fixture(scope="module")
def realms_ir():
    return compile_files([REALMS])


@pytest.fixture(scope="module")
def tenants_ir():
    return compile_files([TENANTS])


# ---------------------------------------------------------- realm discovery

def test_realms_of_lists_named_realms(realms_ir):
    assert erase_report.realms_of(realms_ir) == ["alpha", "beta"]


def test_unknown_realm_is_a_clean_error(realms_ir):
    report = erase_report.build_report(realms_ir, "ghost", prove_residue=False)
    assert report["ok"] is False
    assert "ghost" in report["error"]
    assert report["knownRealms"] == ["alpha", "beta"]


# ---------------------------------------------------------- honest scope

def test_honest_scope_header_is_present(realms_ir):
    report = erase_report.build_report(realms_ir, "alpha", prove_residue=False)
    scope = report["honestScope"]
    # the header must state BOTH what it proves and what it explicitly does not
    assert scope["proves"] and scope["doesNotProve"]
    blob = " ".join(scope["doesNotProve"]).lower()
    assert "compensation is not inversion" in blob
    assert "§6.1" in " ".join(scope["doesNotProve"]) or "6.1" in scope["reference"]
    # enumerates exposure, does not undo it
    assert "enumerates" in blob and "does not undo" in blob


def test_render_leads_with_scope_and_names_the_realm(realms_ir):
    report = erase_report.build_report(realms_ir, "alpha", prove_residue=False)
    text = erase_report.render(report)
    assert "REALM ERASURE REPORT — realm `alpha`" in text
    assert "DOES NOT PROVE" in text
    assert "compensation is NOT inversion" in text


# ---------------------------------------------------------- section 2: crossings

def test_bare_crossing_is_listed_as_bare(realms_ir):
    report = erase_report.build_report(realms_ir, "alpha", prove_residue=False)
    cross = report["boundaryCrossings"]
    by_method = {e["method"]: e for e in cross["emissions"]}
    # `sink.bare` is emitted with no compensate clause -> bare
    assert by_method["bare"]["compensated"] is False
    assert "emit:AlphaApp:sink.bare" in cross["bareTokens"]
    # `sink.commit` is emitted WITH a compensate clause -> compensated
    assert by_method["commit"]["compensated"] is True
    assert cross["bareCount"] >= 1 and cross["compensatedCount"] >= 1


def test_fully_revertible_realm_has_no_crossings(tenants_ir):
    report = erase_report.build_report(tenants_ir, "tenant_a", prove_residue=False)
    cross = report["boundaryCrossings"]
    assert cross["total"] == 0
    assert cross["bareCount"] == 0 and cross["compensatedCount"] == 0


# ---------------------------------------------------------- section 3: survivors

def test_other_realms_proven_untouched_via_survivors(realms_ir):
    report = erase_report.build_report(realms_ir, "alpha", prove_residue=False)
    others = report["otherRealmsUntouched"]
    assert others["untouched"] is True
    assert others["breached"] == []
    # withdrawing realm alpha withdraws exactly its two components
    assert others["withdrawnComponents"] == ["AlphaSink", "AlphaApp"]
    # and beta's components keep every provision — that IS the proof
    assert others["survivors"] == ["BetaApp", "BetaSink"]
    assert others["otherRealms"]["beta"] == ["BetaApp", "BetaSink"]


def test_survivors_on_tenants_example(tenants_ir):
    report = erase_report.build_report(tenants_ir, "tenant_a", prove_residue=False)
    others = report["otherRealmsUntouched"]
    assert others["untouched"] is True
    assert others["survivors"] == ["TenantBApp", "TenantBStore"]


# ---------------------------------------------------------- section 1: state gone

def test_state_gone_lists_the_realm_provisions(realms_ir):
    report = erase_report.build_report(realms_ir, "alpha", prove_residue=False)
    prov = report["inProcessStateGone"]["provisionsErased"]
    assert {p["provider"] for p in prov} == {"AlphaSink"}
    assert prov[0]["key"] == "sink" and prov[0]["realm"] == "alpha"


def test_static_report_skips_the_runtime_proof(realms_ir):
    report = erase_report.build_report(realms_ir, "alpha", prove_residue=False)
    residue = report["inProcessStateGone"]["noResidueProof"]
    assert residue["available"] is False
    assert residue["proven"] is None


@pytest.mark.skipif(not _has_runtime(), reason="cordis-py runtime not installed")
def test_no_residue_proof_holds_over_a_real_teardown(realms_ir):
    report = erase_report.build_report(realms_ir, "alpha", prove_residue=True)
    residue = report["inProcessStateGone"]["noResidueProof"]
    assert residue["available"] is True
    assert residue["proven"] is True
    # the four R4 checks: registry, provisions, effect disposables, listeners
    assert all(residue["checks"].values())
    assert set(residue["checks"]) == {"registry", "provisions", "effects", "listeners"}
    assert report["summary"]["stateGoneProven"] is True


# ---------------------------------------------------------- versioned document

def test_report_is_a_versioned_self_describing_document(realms_ir):
    report = erase_report.build_report(realms_ir, "beta", prove_residue=False)
    assert report["kind"] == "revl.erase-report"
    assert report["schema_version"] == "1.0"
    assert report["realm"] == "beta"
    # round-trips through JSON without loss (it is an interchange artifact)
    assert json.loads(json.dumps(report)) == report


# ---------------------------------------------------------- CLI wiring

def test_cli_static_report_json(realms_ir):
    proc = subprocess.run(
        [sys.executable, "-m", "revl", "erase-report", REALMS,
         "--realm", "alpha", "--json", "--no-residue-proof"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")},
    )
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["ok"] and doc["realm"] == "alpha"
    assert doc["kind"] == "revl.erase-report"
    assert doc["summary"]["bareCrossings"] >= 1


def test_cli_unknown_realm_exits_nonzero():
    proc = subprocess.run(
        [sys.executable, "-m", "revl", "erase-report", REALMS,
         "--realm", "ghost", "--no-residue-proof"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")},
    )
    assert proc.returncode == 1
    assert "unknown realm" in proc.stdout + proc.stderr

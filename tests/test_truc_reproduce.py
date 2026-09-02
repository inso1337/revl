"""`truc reproduce <component@version>`, deterministic package reproduction
(roadmap item 297).

These drive the REAL reproduce machinery (`revl.truc.reproduce`) against a
throwaway copy of the repo's own `registry/`. They are frontend-only, the
reproduction path is `compile_files` + `registry` + `attest`, the same path
`registry.build_index` runs, so nothing here needs the cordis runtime and it
runs in the ordinary `pytest tests/` suite. No mocks: a real component is
rebuilt through the real compiler and its recomputed hashes are compared against
what the registry recorded.

The four properties item 297 asks for:

  * a published component reproduces GREEN, every recorded tier OK;
  * a deliberately-tampered recorded hash reports the EXACT tier as MISMATCH,
    with both the recorded and the rebuilt hash;
  * a missing lock / attestation reports `cannot verify`, never a crash;
  * an attestation (item 127) and a recorded emitted artifact are reproduced
    too, the gap between "source verified" and "artifact reproducible".
"""

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# The backend emitters live under <root>/backends and are not on the default
# path; add ROOT so the emitted-artifact tier can actually re-emit here (in a
# bare install they are absent and the tier degrades with a reason instead).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from revl import attest  # noqa: E402
from revl.compiler import compile_files  # noqa: E402
from revl.truc import reproduce as R  # noqa: E402

REGISTRY = ROOT / "registry"
COMPONENT = "user_cache"


@pytest.fixture
def registry(tmp_path):
    """A throwaway copy of the repo registry, so a test may tamper with a
    recorded hash without touching the committed one."""
    dst = tmp_path / "registry"
    shutil.copytree(REGISTRY, dst)
    return dst


def _entry(registry: Path) -> Path:
    return registry / "components" / COMPONENT


def _tier(report, name: str):
    for c in report.checks:
        if c.tier == name:
            return c
    return None


# ------------------------------------------------------------------- green

def test_reproduce_is_green_for_a_published_component(registry):
    """The recorded component rebuilds bit-for-bit: every tier that has recorded
    evidence is OK, none is a MISMATCH, and the overall verdict is reproduced."""
    report = R.reproduce(COMPONENT, registry=str(registry))
    assert report.ok, [(c.tier, c.status, c.detail) for c in report.mismatches]
    assert not report.mismatches
    for tier in (R.TIER_SOURCE, R.TIER_LOCK, R.TIER_IR, R.TIER_POLICY,
                 R.TIER_BACKEND):
        assert _tier(report, tier).status == R.OK, tier
    # nothing recorded for these in a plain registry entry -> honest cannot-verify
    assert _tier(report, R.TIER_ATTESTATION).status == R.UNVERIFIED
    assert _tier(report, R.TIER_ARTIFACT).status == R.UNVERIFIED


def test_render_names_the_verdict(registry):
    report = R.reproduce(COMPONENT, registry=str(registry))
    text = R.render(report)
    assert "reproduced:" in text and COMPONENT in text
    assert "MISMATCH" not in text


# --------------------------------------------------------------- tamper cases

def test_tampered_source_hash_is_an_exact_source_mismatch(registry):
    """Corrupting the recorded `sourceHash` makes exactly the source tier a
    MISMATCH, with the recorded and rebuilt hashes both surfaced."""
    index_path = registry / "index.json"
    idx = json.loads(index_path.read_text())
    idx["components"][COMPONENT]["sourceHash"] = "dead" * 16
    index_path.write_text(json.dumps(idx, indent=2, sort_keys=True) + "\n")

    report = R.reproduce(COMPONENT, registry=str(registry))
    assert not report.ok
    src = _tier(report, R.TIER_SOURCE)
    assert src.status == R.MISMATCH
    assert src.recorded.startswith("dead")
    assert src.rebuilt and src.rebuilt != src.recorded
    # the divergence is isolated: the IR/policy tiers still hold.
    assert _tier(report, R.TIER_IR).status == R.OK
    assert [c.tier for c in report.mismatches] == [R.TIER_SOURCE]


def test_tampered_manifest_hash_is_an_exact_ir_mismatch(registry):
    """Corrupting the recorded `manifestHash` makes exactly the IR tier a
    MISMATCH, a source that hashes right but an IR that does not is precisely
    the gap reproduce closes."""
    index_path = registry / "index.json"
    idx = json.loads(index_path.read_text())
    idx["components"][COMPONENT]["manifestHash"] = "beef" * 16
    index_path.write_text(json.dumps(idx, indent=2, sort_keys=True) + "\n")

    report = R.reproduce(COMPONENT, registry=str(registry))
    assert not report.ok
    ir = _tier(report, R.TIER_IR)
    assert ir.status == R.MISMATCH
    assert ir.recorded.startswith("beef") and ir.rebuilt
    assert _tier(report, R.TIER_SOURCE).status == R.OK
    assert "diverged on IR" in R.render(report)


def test_tampered_policy_surface_is_a_policy_mismatch(registry):
    """Corrupting the recorded emission count makes the policy-surface tier a
    MISMATCH (the G4 surface a consumer trusts a ship to have frozen)."""
    index_path = registry / "index.json"
    idx = json.loads(index_path.read_text())
    idx["components"][COMPONENT]["emissions"] = 999
    index_path.write_text(json.dumps(idx, indent=2, sort_keys=True) + "\n")

    report = R.reproduce(COMPONENT, registry=str(registry))
    assert not report.ok
    assert _tier(report, R.TIER_POLICY).status == R.MISMATCH


# --------------------------------------------------------- missing-record cases

def test_missing_attestation_is_cannot_verify_not_a_crash(registry):
    """A component with no recorded attestation reports `cannot verify` for that
    tier and still reproduces on the tiers it can, never a crash."""
    report = R.reproduce(COMPONENT, registry=str(registry))
    att = _tier(report, R.TIER_ATTESTATION)
    assert att.status == R.UNVERIFIED
    assert "no recorded attestation" in att.detail
    assert report.ok  # the unverifiable tier does not fail the rebuild


def test_missing_registry_row_is_a_clean_error(registry):
    """Reproducing a name the registry does not carry is a clean RevlError
    (surfaced by the CLI as `cannot verify: ...`), not a traceback."""
    from revl.errors import RevlError

    with pytest.raises(RevlError) as excinfo:
        R.reproduce("not_a_real_component", registry=str(registry))
    assert "not in this registry" in excinfo.value.message


def test_requested_version_with_no_recorded_version_is_unverifiable(registry):
    """The registry index carries no per-component version, so `name@1.2.0`
    reports the version tier as `cannot verify` and reproduces the published
    component regardless, never a false version match."""
    report = R.reproduce(f"{COMPONENT}@1.2.0", registry=str(registry))
    version_check = _tier(report, "version")
    assert version_check is not None
    assert version_check.status == R.UNVERIFIED
    assert report.version == "1.2.0"
    assert report.ok


# ------------------------------------------------------------- attestation tier

def _attest_the_entry(registry: Path, key: bytes) -> None:
    """Sign the entry's rebuilt (canonical) IR with `key` and drop the
    attestation next to the component, the way a publish would."""
    verdict = attest.run_gate(paths=[str(_entry(registry) / "component.rvl")],
                              normalize=R._normalized_ir)
    att = attest.make_attestation(R._normalized_ir(verdict.ir), key,
                                  verdict=verdict)
    (_entry(registry) / "attestation.json").write_text(json.dumps(att, indent=2))


def test_attestation_verifies_green(registry, monkeypatch):
    """A recorded attestation signed over the same source verifies OK: the
    signature is authentic and the rebuilt IR matches the signed hash."""
    monkeypatch.setenv(attest.KEY_ENV, "a-shared-secret")
    _attest_the_entry(registry, b"a-shared-secret")

    report = R.reproduce(COMPONENT, registry=str(registry))
    att = _tier(report, R.TIER_ATTESTATION)
    assert att.status == R.OK, att.detail
    assert report.ok


def test_tampered_attestation_is_a_mismatch(registry, monkeypatch):
    """Altering a signed field (the verdict) after signing is caught as an
    attestation MISMATCH, a signature failure, exactly the item-127 contract."""
    monkeypatch.setenv(attest.KEY_ENV, "a-shared-secret")
    _attest_the_entry(registry, b"a-shared-secret")
    att_path = _entry(registry) / "attestation.json"
    doc = json.loads(att_path.read_text())
    doc["verdict"] = "tampered"
    att_path.write_text(json.dumps(doc, indent=2))

    report = R.reproduce(COMPONENT, registry=str(registry))
    assert not report.ok
    assert _tier(report, R.TIER_ATTESTATION).status == R.MISMATCH


def test_attestation_present_but_no_key_is_cannot_verify(registry, monkeypatch):
    """An attestation is recorded but no signing key is available: honest
    `cannot verify`, not a false pass and not a crash."""
    monkeypatch.delenv(attest.KEY_ENV, raising=False)
    monkeypatch.delenv(attest.KEY_FILE_ENV, raising=False)
    _attest_the_entry(registry, b"a-shared-secret")

    report = R.reproduce(COMPONENT, registry=str(registry))
    att = _tier(report, R.TIER_ATTESTATION)
    assert att.status == R.UNVERIFIED
    assert "no signing key" in att.detail


# --------------------------------------------------------- emitted-artifact tier

def _record_py_artifact(registry: Path, digest: str) -> None:
    (_entry(registry) / "artifacts.json").write_text(
        json.dumps({"backends": {"python": {"sourceSha256": digest}}}))


def test_emitted_artifact_reproduces_green(registry):
    """When the entry records the python-emitter output hash, reproduce re-emits
    the backend source from the rebuilt IR and matches it byte-for-byte."""
    from revl.registry import _sha256

    ir = compile_files([str(_entry(registry) / "component.rvl")])
    emitted = R._emit_backend_source("python", ir)
    if emitted is None:
        pytest.skip("python backend emitter not importable in this environment")
    _record_py_artifact(registry, _sha256(emitted))

    report = R.reproduce(COMPONENT, registry=str(registry))
    art = _tier(report, f"{R.TIER_ARTIFACT} [python]")
    assert art is not None and art.status == R.OK, art
    assert report.ok


def test_emitted_artifact_mismatch_is_reported(registry):
    """A recorded artifact hash that the re-emit does not reproduce is a
    MISMATCH with both hashes, the artifact-reproducibility gap, caught."""
    ir = compile_files([str(_entry(registry) / "component.rvl")])
    if R._emit_backend_source("python", ir) is None:
        pytest.skip("python backend emitter not importable in this environment")
    _record_py_artifact(registry, "00" * 32)

    report = R.reproduce(COMPONENT, registry=str(registry))
    art = _tier(report, f"{R.TIER_ARTIFACT} [python]")
    assert art is not None and art.status == R.MISMATCH
    assert art.recorded == "00" * 32 and art.rebuilt
    assert not report.ok


# ------------------------------------------------------------------- CLI wiring

def test_cli_run_returns_zero_on_green(registry, capsys):
    code = R.run([COMPONENT, "--registry", str(registry)])
    assert code == 0
    assert "reproduced:" in capsys.readouterr().out


def test_cli_run_returns_one_on_mismatch(registry, capsys):
    index_path = registry / "index.json"
    idx = json.loads(index_path.read_text())
    idx["components"][COMPONENT]["sourceHash"] = "dead" * 16
    index_path.write_text(json.dumps(idx, indent=2, sort_keys=True) + "\n")

    code = R.run([COMPONENT, "--registry", str(registry)])
    assert code == 1
    assert "NOT reproduced" in capsys.readouterr().out


def test_cli_run_missing_component_arg_is_usage_exit_two(capsys):
    code = R.run([])
    assert code == 2
    assert "component name is required" in capsys.readouterr().out


def test_cli_run_unknown_component_is_cannot_verify_exit_two(registry, capsys):
    code = R.run(["not_a_real_component", "--registry", str(registry)])
    assert code == 2
    assert "cannot verify" in capsys.readouterr().out

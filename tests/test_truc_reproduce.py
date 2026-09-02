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
    evidence is OK and none is a MISMATCH.

    It is NOT `fully_verified`, and the report says so. Three tiers had nothing
    to check — no attestation, no recorded artifact, and (reproducing straight
    out of the registry) no truc.lock pin to anchor the registry against
    anything but itself. `ok` means "nothing diverged", which is a much weaker
    claim than "everything agreed"; a tier that verified nothing is not a pass.
    """
    report = R.reproduce(COMPONENT, registry=str(registry))
    assert report.ok, [(c.tier, c.status, c.detail) for c in report.mismatches]
    assert not report.mismatches
    for tier in (R.TIER_SOURCE, R.TIER_LOCK, R.TIER_IR, R.TIER_POLICY,
                 R.TIER_BACKEND):
        assert _tier(report, tier).status == R.OK, tier
    # nothing recorded for these in a plain registry entry -> honest cannot-verify
    assert _tier(report, R.TIER_ATTESTATION).status == R.UNVERIFIED
    assert _tier(report, R.TIER_ARTIFACT).status == R.UNVERIFIED
    assert _tier(report, R.TIER_ANCHOR).status == R.UNVERIFIED
    assert not report.fully_verified
    assert report.verdict == "partially reproduced"


def test_render_names_the_verdict(registry):
    report = R.reproduce(COMPONENT, registry=str(registry))
    text = R.render(report)
    assert "reproduced:" in text and COMPONENT in text
    assert "MISMATCH" not in text
    # and it never claims a bit-for-bit rebuild it did not establish.
    assert "partially reproduced:" in text
    assert "not proof of reproduction" in text
    assert "rebuilds bit-for-bit" not in text


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
    # the tier names the path it looked at, so "nothing there" is checkable
    # rather than something a reader has to take on faith.
    assert "evidence/attestation.json" in att.detail
    assert report.ok  # the unverifiable tier does not fail the rebuild
    assert not report.fully_verified  # but it does not count as verified either


def test_missing_registry_row_is_a_clean_error(registry):
    """Reproducing a name the registry does not carry is a clean RevlError
    (surfaced by the CLI as `cannot verify: ...`), not a traceback."""
    from revl.errors import RevlError

    with pytest.raises(RevlError) as excinfo:
        R.reproduce("not_a_real_component", registry=str(registry))
    assert "not in this registry" in excinfo.value.message


def test_a_requested_version_that_cannot_be_checked_refuses(registry):
    """Roadmap 428 F12. The registry index carries no per-component version, so
    `@version` is not a pin. It used to report the tier `cannot verify`, and an
    unverifiable tier contributes no MISMATCH, so
    `truc reproduce name@99.99.99-totally-different` reproduced whatever `name`
    is TODAY and answered `ok=True`: an honest report line under a dishonest
    verdict, because the caller asked about one version and was answered about
    another.

    This test previously pinned that behaviour as correct.
    """
    report = R.reproduce(f"{COMPONENT}@99.99.99-totally-different",
                         registry=str(registry))
    version_check = _tier(report, "version")
    assert version_check is not None
    assert version_check.status == R.MISMATCH
    assert "is not a pin" in version_check.detail
    assert report.version == "99.99.99-totally-different"
    assert report.ok is False
    assert report.verdict == "not reproduced"


def test_an_unversioned_request_still_reproduces(registry):
    """The refusal is scoped to a version that was ASKED FOR and cannot be
    checked. Asking for the component itself is unchanged."""
    report = R.reproduce(COMPONENT, registry=str(registry))
    assert _tier(report, "version") is None
    assert report.ok


# ------------------------------------------------------------- attestation tier

def _attest_the_entry(registry: Path, key: bytes, *, legacy: bool = False) -> Path:
    """Sign the entry's rebuilt (canonical) IR with `key` and drop the
    attestation where a publish actually puts it.

    That is `<entry>/evidence/attestation.json`, the path
    `registry.build_evidence` writes. This helper used to write
    `<entry>/attestation.json` — the same path the tier used to read — so the
    attestation tier tested green against a location nothing publishes to,
    while being structurally dead for every real entry. `legacy=True` writes the
    bare root path, still honoured for entries published before item 293.
    """
    verdict = attest.run_gate(paths=[str(_entry(registry) / "component.rvl")],
                              normalize=R._normalized_ir)
    att = attest.make_attestation(R._normalized_ir(verdict.ir), key,
                                  verdict=verdict)
    if legacy:
        target = _entry(registry) / "attestation.json"
    else:
        evidence = _entry(registry) / "evidence"
        evidence.mkdir(exist_ok=True)
        target = evidence / "attestation.json"
    target.write_text(json.dumps(att, indent=2))
    return target


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
    att_path = _attest_the_entry(registry, b"a-shared-secret")
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


# ------------------------------------------- attestation tier: the published path

def test_the_attestation_tier_finds_what_build_evidence_publishes(registry,
                                                                  monkeypatch):
    """The F6 regression. `registry.build_evidence` writes the attestation to
    `<entry>/evidence/attestation.json`; the tier read `<entry>/attestation.json`,
    a path nothing writes. So for every entry the real publish path produces the
    tier was structurally DEAD — it could only ever say "no recorded
    attestation", and an unverifiable tier does not fail a rebuild, so a dead
    tier read as a pass. Drive the real publisher and require the tier to
    actually find and verify the file."""
    key = b"published-evidence-key"
    monkeypatch.setenv(attest.KEY_ENV, "published-evidence-key")
    from revl import registry as R_registry

    R_registry.build_evidence(str(registry), key=key, signer="revl-ci")
    published = (_entry(registry) / R_registry.EVIDENCE_DIRNAME
                 / R_registry.EVIDENCE_ATTESTATION)
    assert published.exists(), "build_evidence must publish here"
    assert not (_entry(registry) / "attestation.json").exists()

    report = R.reproduce(COMPONENT, registry=str(registry))
    att = _tier(report, R.TIER_ATTESTATION)
    assert att.status == R.OK, att.detail
    assert "no recorded attestation" not in att.detail


def test_a_forged_bound_dossier_is_an_attestation_mismatch(registry, monkeypatch):
    """`build_evidence` binds each dossier's hash into the signed attestation.
    Swapping a bound dossier afterwards leaves the signature authentic but the
    binding broken, and the tier must call that a MISMATCH rather than verifying
    the signature and calling it a day."""
    key = b"published-evidence-key"
    monkeypatch.setenv(attest.KEY_ENV, "published-evidence-key")
    from revl import registry as R_registry

    R_registry.build_evidence(str(registry), key=key, signer="revl-ci")
    capabilities = (_entry(registry) / R_registry.EVIDENCE_DIRNAME
                    / R_registry.EVIDENCE_CAPABILITIES)
    capabilities.write_text(json.dumps(
        {"kind": "revl.capabilities", "boundary": {}}, indent=2, sort_keys=True))

    report = R.reproduce(COMPONENT, registry=str(registry))
    att = _tier(report, R.TIER_ATTESTATION)
    assert att.status == R.MISMATCH, att.detail
    assert "capabilities" in att.detail
    assert not report.ok


def test_the_legacy_root_attestation_is_still_honoured(registry, monkeypatch):
    """Entries published before item 293 wrote the attestation at the entry
    root. Pointing the tier at the bundle path must not orphan them."""
    monkeypatch.setenv(attest.KEY_ENV, "a-shared-secret")
    path = _attest_the_entry(registry, b"a-shared-secret", legacy=True)
    assert path == _entry(registry) / "attestation.json"

    report = R.reproduce(COMPONENT, registry=str(registry))
    assert _tier(report, R.TIER_ATTESTATION).status == R.OK


# --------------------------------------- independent pin: the registry as adversary

def _project_with(tmp_path: Path, registry: Path, name: str,
                  source_hash: str | None) -> Path:
    """A project that vendored `name` out of `registry` and pinned it. Passing
    `source_hash=None` writes the honest pin; passing a value overrides it."""
    import hashlib

    project = tmp_path / "app"
    (project / "trucs" / name).mkdir(parents=True)
    source = (registry / "components" / name / "component.rvl").read_text()
    (project / "trucs" / name / "component.rvl").write_text(source)
    pin = (hashlib.sha256(source.encode()).hexdigest()
           if source_hash is None else source_hash)
    (project / "truc.lock").write_text(json.dumps(
        {"lockVersion": 0, "trucs": [{"name": name, "sourceHash": pin}]},
        indent=2))
    return project


def test_a_substituted_dependency_is_caught_by_the_independent_pin(registry,
                                                                   tmp_path):
    """When the REGISTRY is the adversary, every registry-internal tier
    reproduces green: substitute the source, regenerate the index over it, and
    source/IR/policy/backend all agree — because they compare the registry with
    itself. The project's truc.lock pin is the one value the registry cannot
    mint, and it catches the swap.

    The pin cross-check itself is not new; it lived inside the source tier,
    where its ABSENCE was invisible (see the no-pin case below). It gets its own
    tier so "there was nothing independent to check against" is a thing the
    report can say."""
    from revl import registry as R_registry

    project = _project_with(tmp_path, registry, COMPONENT, None)

    # the adversary: swap the published source, then re-index over it so the
    # registry is perfectly self-consistent again.
    entry_source = _entry(registry) / "component.rvl"
    entry_source.write_text(entry_source.read_text() + "\n// substituted\n")
    R_registry.build_index(str(registry))
    assert R_registry.verify(str(registry)) == []   # the registry looks pristine

    report = R.reproduce(COMPONENT, project_dir=str(project),
                         registry=str(registry))
    # every self-referential tier is still green ...
    for tier in (R.TIER_SOURCE, R.TIER_IR, R.TIER_POLICY, R.TIER_BACKEND):
        assert _tier(report, tier).status == R.OK, tier
    # ... and the independent one is not.
    anchor = _tier(report, R.TIER_ANCHOR)
    assert anchor.status == R.MISMATCH, anchor.detail
    assert "substituted dependency" in anchor.detail
    assert not report.ok


def test_a_substituted_dependency_with_no_pin_was_invisible(registry, tmp_path):
    """The same substitution, against a project that vendored the truc but never
    pinned it — the state F3 used to allow. Every tier then compares the registry
    with itself, the reproduce comes back entirely green, and nothing anywhere
    notices that the vendored dependency was replaced. An unpinned vendored truc
    is now a MISMATCH in its own right: the report says what it could not check
    instead of passing for lack of anything to check against."""
    from revl import registry as R_registry

    project = _project_with(tmp_path, registry, COMPONENT, None)
    (project / "truc.lock").unlink()          # no pin at all

    entry_source = _entry(registry) / "component.rvl"
    entry_source.write_text(entry_source.read_text() + "\n// substituted\n")
    R_registry.build_index(str(registry))

    report = R.reproduce(COMPONENT, project_dir=str(project),
                         registry=str(registry))
    assert _tier(report, R.TIER_SOURCE).status == R.OK   # self-consistent registry
    anchor = _tier(report, R.TIER_ANCHOR)
    assert anchor.status == R.MISMATCH, anchor.detail
    assert "no truc.lock row" in anchor.detail
    assert not report.ok


def test_an_honest_pin_makes_the_anchor_tier_green(registry, tmp_path):
    """The anchor tier is not a blanket refusal: a project whose pin agrees with
    the registry's source and with its own vendored bytes reports OK."""
    project = _project_with(tmp_path, registry, COMPONENT, None)
    report = R.reproduce(COMPONENT, project_dir=str(project),
                         registry=str(registry))
    anchor = _tier(report, R.TIER_ANCHOR)
    assert anchor.status == R.OK, anchor.detail
    assert "vendored copy" in anchor.detail


def test_a_blank_pin_is_a_mismatch_not_a_shrug(registry, tmp_path):
    """A lock row with a blank `sourceHash` anchors nothing. Reporting it as
    fine is how an unpinned dependency stays invisible."""
    project = _project_with(tmp_path, registry, COMPONENT, "")
    report = R.reproduce(COMPONENT, project_dir=str(project),
                         registry=str(registry))
    anchor = _tier(report, R.TIER_ANCHOR)
    assert anchor.status == R.MISMATCH
    assert "blank" in anchor.detail
    assert not report.ok


def test_a_vendored_truc_with_no_lock_row_is_a_mismatch(registry, tmp_path):
    """Vendored, used, and pinned by nothing."""
    project = _project_with(tmp_path, registry, COMPONENT, None)
    (project / "truc.lock").write_text(json.dumps(
        {"lockVersion": 0, "trucs": []}, indent=2))
    report = R.reproduce(COMPONENT, project_dir=str(project),
                         registry=str(registry))
    anchor = _tier(report, R.TIER_ANCHOR)
    assert anchor.status == R.MISMATCH
    assert "no truc.lock row" in anchor.detail


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


def test_cli_strict_refuses_to_pass_an_unverifiable_tier(registry, capsys):
    """`--strict` is the switch that says a tier which checked nothing is not a
    pass. Same rebuild, same absence of any MISMATCH — exit 1."""
    code = R.run([COMPONENT, "--registry", str(registry), "--strict"])
    assert code == 1
    out = capsys.readouterr().out
    assert "partially reproduced" in out
    assert "MISMATCH" not in out


def test_cli_json_reports_what_was_not_verified(registry, capsys):
    code = R.run([COMPONENT, "--registry", str(registry), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["reproduced"] is True
    assert doc["fullyVerified"] is False
    assert doc["verdict"] == "partially reproduced"
    assert R.TIER_ATTESTATION in doc["unverified"]
    assert R.TIER_ANCHOR in doc["unverified"]


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

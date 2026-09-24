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


def _unversion(registry: Path) -> None:
    """Strip the entry's declared version and re-record the index row, i.e. an
    entry published by a registry that declares no versions at all."""
    from revl.registry import ENTRY_VERSION_FILENAME, build_index

    (_entry(registry) / ENTRY_VERSION_FILENAME).unlink()
    build_index(registry)


def test_a_requested_version_no_registry_records_refuses(registry):
    """Roadmap 428 F12, the original shape: against a registry that records no
    per-component version, `@version` has nothing to be checked against.

    It used to report the tier `cannot verify`, and an unverifiable tier
    contributes no MISMATCH, so
    `truc reproduce name@99.99.99-totally-different` reproduced whatever `name`
    is TODAY and answered `ok=True`: an honest report line under a dishonest
    verdict, because the caller asked about one version and was answered about
    another. This test previously pinned that behaviour as correct.
    """
    _unversion(registry)
    report = R.reproduce(f"{COMPONENT}@99.99.99-totally-different",
                         registry=str(registry))
    version_check = _tier(report, R.TIER_VERSION)
    assert version_check is not None
    assert version_check.status == R.MISMATCH
    assert "is not a pin" in version_check.detail
    assert report.version == "99.99.99-totally-different"
    assert report.ok is False
    assert report.verdict == "not reproduced"


def test_a_recorded_version_that_matches_is_a_live_pin(registry):
    """428 F12, the residual half: `@version` now PINS. The registry records a
    per-component version, so a request that names it is checked and the tier
    is OK with both values surfaced — the first time this tier verifies
    anything rather than degrading."""
    report = R.reproduce(f"{COMPONENT}@1.0.0", registry=str(registry))
    check = _tier(report, R.TIER_VERSION)
    assert check.status == R.OK
    assert check.recorded == "1.0.0" and check.rebuilt == "1.0.0"
    assert report.ok


def test_a_recorded_version_that_disagrees_refuses_the_resolution(registry):
    """A pin the registry cannot honour is not reproduced under a warning: the
    resolution itself refuses, because rebuilding a different release under the
    name that was asked for answers a different question."""
    from revl.errors import RevlError

    with pytest.raises(RevlError) as excinfo:
        R.reproduce(f"{COMPONENT}@2.0.0", registry=str(registry))
    assert "is not published here" in excinfo.value.message
    assert "1.0.0" in excinfo.value.message


def test_an_unversioned_request_is_not_fully_reproduced(registry):
    """Asking for no version is answered with no pin, not with a silent pass.

    A rebuild that named no release reproduced "whatever this registry serves
    right now", which is a real but much weaker claim than reproducing a named
    release, so the version tier is `cannot verify` and the run can never be
    *fully* reproduced. It is not a failure: no tier diverged, so `ok` holds,
    the same honest-degradation rule every other tier follows. The detail names
    the version that WOULD pin it."""
    report = R.reproduce(COMPONENT, registry=str(registry))
    check = _tier(report, R.TIER_VERSION)
    assert check.status == R.UNVERIFIED
    assert "no release was pinned" in check.detail
    assert f"truc reproduce {COMPONENT}@1.0.0" in check.detail
    assert report.ok
    assert not report.fully_verified


def test_an_unversioned_registry_cannot_pin_at_all(registry):
    """An entry that declares no version is honestly unversioned: nothing is
    invented for it, the tier says there is no release identity to pin, and the
    run is at best partially reproduced."""
    _unversion(registry)
    report = R.reproduce(COMPONENT, registry=str(registry))
    check = _tier(report, R.TIER_VERSION)
    assert check.status == R.UNVERIFIED
    assert "records none" in check.detail
    assert report.ok and not report.fully_verified


def test_a_malformed_declared_version_refuses_to_publish(registry):
    """Fail-closed at the publish end. A `version` file that is present but
    unusable is a refusal, never a silently unversioned entry: a publisher who
    meant to declare a version must not ship one a consumer can never pin."""
    from revl.errors import RevlError
    from revl.registry import ENTRY_VERSION_FILENAME, build_index

    (_entry(registry) / ENTRY_VERSION_FILENAME).write_text("not a version\n")
    with pytest.raises(RevlError) as excinfo:
        build_index(registry)
    assert "must be one token" in excinfo.value.message


def test_a_relabelled_release_is_caught_by_the_independent_pin(tmp_path, registry):
    """The registry-as-adversary case for the version, one tier over. A project
    that locked `name` at one version sees the registry serving the same name at
    another — even with the bytes untouched, so no hash tier moves — because the
    lock row carries the version the project admitted and the registry cannot
    mint that copy."""
    from revl.registry import _sha256

    source = (_entry(registry) / "component.rvl").read_text()
    project = tmp_path / "proj"
    (project / "trucs" / COMPONENT).mkdir(parents=True)
    (project / "trucs" / COMPONENT / "component.rvl").write_text(source)
    (project / "truc.lock").write_text(json.dumps({
        "lockVersion": 0,
        "trucs": [{"name": COMPONENT, "version": "0.9.0",
                   "sourceHash": _sha256(source)}]}))

    report = R.reproduce(COMPONENT, project_dir=str(project),
                         registry=str(registry))
    anchor = _tier(report, R.TIER_ANCHOR)
    assert anchor.status == R.MISMATCH
    assert "relabelled" in anchor.detail
    assert anchor.recorded == "0.9.0" and anchor.rebuilt == "1.0.0"
    assert not report.ok


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
    emitted, refusal = R._emit_backend_source("python", ir)
    assert refusal is None, refusal
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
    if R._emit_backend_source("python", ir)[0] is None:
        pytest.skip("python backend emitter not importable in this environment")
    _record_py_artifact(registry, "00" * 32)

    report = R.reproduce(COMPONENT, registry=str(registry))
    art = _tier(report, f"{R.TIER_ARTIFACT} [python]")
    assert art is not None and art.status == R.MISMATCH
    assert art.recorded == "00" * 32 and art.rebuilt
    assert not report.ok
    # A byte mismatch carries a REBUILT hash. The refusal MISMATCH below carries
    # none, and that is how a consumer tells the two apart (issue #1403).
    assert "refuses this IR" not in art.detail


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


# ------------------------------- issue #1403: refusal, fault and absence apart

"""`_emit_backend_source` used to catch `ImportError` and nothing else, so an
emitter that REFUSED the rebuilt IR escaped `truc reproduce` as a raw Python
traceback (12 frames, measured on the worktree base 2a0b9758a). That is the
attestation path: it is what turns a published component into a claim someone
who did not build it can re-check, and a traceback there does not say whether
the artifact is wrong, the compiler moved, or the tier refuses the document.

Three outcomes, kept apart:

  * the emitter is ABSENT here        -> `cannot verify`, nothing was decided;
  * the emitter REFUSES this IR       -> MISMATCH quoting the refusal, no
                                         invented rebuilt hash;
  * the emitter FAULTS                -> a traceback, unchanged and uncaught.

THE CONTROL CARRIES AS MUCH WEIGHT AS THE FIX. `EmitError` subclasses
`ValueError`, so a test that only asserts "no traceback" passes just as well
against a broadened `except ValueError` that swallows genuine emitter crashes -
the exact pattern PR #1402 removes from `bundle.py`. Every assertion below is
paired with a bare-`ValueError` control, the same way PRs #1399 and #1402 pair
theirs.
"""

# A document the python tier refuses BY NAME: a `validated` extern has no
# crossing this tier can check (items 257/513, issue #1382). It compiles and
# admits, so every tier before the artifact one is green and the refusal is the
# only thing under test.
REFUSED_BY_PY = """
type Call = { tool: Str, args: Str }
type AgentTurn = Final(Str) | ToolCalls(List[Call])

extern emission[model] validated fn complete(h: Str) -> AgentTurn = @py {
  return {"kind": "Final", "value": "x"}
}

service Cache {
  fn get(k: Str) -> Str
}

component UserCache provides cache: Cache {
  provide cache {
    fn get(k) = k
  }
}
"""


def _py_emit_module():
    """The python backend's emitter module, or a skip. Imported exactly the way
    `_emit_backend_source` imports it, so the `EmitError` class here is the one
    that instance will be raised from."""
    import importlib  # noqa: PLC0415

    try:
        return importlib.import_module("backends.python.emit")
    except ImportError:  # pragma: no cover - a bare install has no backends/
        pytest.skip("python backend emitter not importable in this environment")


@pytest.fixture
def refusing_registry(registry):
    """The throwaway registry, republished with a source the python tier
    refuses. The registry is rebuilt with `build_index`, so the source / IR /
    policy tiers all still agree: the entry is honestly published, it is the
    emitter that will not lower it. Nothing under the repo's own
    `registry/components/` is touched; this is the tmp_path copy."""
    from revl.registry import build_index  # noqa: PLC0415

    (_entry(registry) / "component.rvl").write_text(REFUSED_BY_PY, encoding="utf-8")
    build_index(registry)
    return registry


def test_a_refused_tier_is_a_named_mismatch_and_not_a_traceback(refusing_registry):
    """The bug, end to end. Before: `backends.python.emit.EmitError` propagated
    out of `reproduce()`. After: one MISMATCH line carrying the emitter's own
    sentence verbatim."""
    module = _py_emit_module()
    ir = compile_files([str(_entry(refusing_registry) / "component.rvl")])
    emitted, refusal = R._emit_backend_source("python", ir)
    if refusal is None:
        pytest.skip("this tier no longer refuses this document")
    assert emitted is None
    assert isinstance(module.EmitError("x"), ValueError)  # why the control exists

    _record_py_artifact(refusing_registry, "00" * 32)
    report = R.reproduce(COMPONENT, registry=str(refusing_registry))

    art = _tier(report, f"{R.TIER_ARTIFACT} [python]")
    assert art is not None and art.status == R.MISMATCH
    assert "the emitter refuses this IR" in art.detail
    assert refusal in art.detail, "the emitter's own words, not a paraphrase"
    # No rebuilt hash is invented: nothing was rebuilt.
    assert art.recorded == "00" * 32
    assert art.rebuilt == ""
    # and every tier that was not about the emitter still holds.
    assert [c.tier for c in report.mismatches] == [f"{R.TIER_ARTIFACT} [python]"]
    assert _tier(report, R.TIER_SOURCE).status == R.OK
    assert _tier(report, R.TIER_IR).status == R.OK


def test_the_refusal_renders_and_exits_one_without_a_traceback(refusing_registry, capsys):
    """What the operator sees. The CLI returns 1 (a recorded claim that no
    longer holds), prints the refusal, and prints `rebuilt (none)` rather than a
    second hash that was never computed."""
    ir = compile_files([str(_entry(refusing_registry) / "component.rvl")])
    if R._emit_backend_source("python", ir)[1] is None:
        pytest.skip("this tier no longer refuses this document")
    _record_py_artifact(refusing_registry, "00" * 32)

    code = R.run([COMPONENT, "--registry", str(refusing_registry)])
    out = capsys.readouterr().out
    assert code == 1
    assert "Traceback" not in out
    assert "the emitter refuses this IR" in out
    assert "rebuilt (none)" in out
    assert "NOT reproduced" in out


def test_a_refusal_is_not_a_cannot_verify(refusing_registry):
    """The distinction this issue is about. `cannot verify` means nothing was
    decided; here the emitter is installed, it ran, and it answered. Reporting a
    definite negative as an unchecked tier would let `truc reproduce` exit 0 on
    a component the toolchain can no longer emit."""
    ir = compile_files([str(_entry(refusing_registry) / "component.rvl")])
    if R._emit_backend_source("python", ir)[1] is None:
        pytest.skip("this tier no longer refuses this document")
    _record_py_artifact(refusing_registry, "00" * 32)

    report = R.reproduce(COMPONENT, registry=str(refusing_registry))
    art = _tier(report, f"{R.TIER_ARTIFACT} [python]")
    assert art.status != R.UNVERIFIED
    assert art.tier not in [c.tier for c in report.unverified]
    assert not report.ok


def test_an_internal_emitter_fault_still_escapes_as_a_fault(registry, monkeypatch):
    """CONTROL. `EmitError` IS a `ValueError`, so the catch added for this issue
    must be the module's own class and nothing wider. A bare `ValueError` out of
    the same call is a compiler bug and has to keep its traceback: a verifier
    that answers a crash with a tidy sentence about the artifact is worse than
    the traceback it replaced."""
    module = _py_emit_module()

    def boom(_ir):
        raise ValueError("an internal emitter fault, not a refusal")

    monkeypatch.setattr(module, "emit", boom)
    ir = compile_files([str(_entry(registry) / "component.rvl")])
    with pytest.raises(ValueError, match="an internal emitter fault"):
        R._emit_backend_source("python", ir)


def test_the_fault_control_reaches_the_whole_reproduce_run(registry, monkeypatch):
    """CONTROL, one level up: the fault is not caught by the artifact tier
    either, so it leaves `reproduce()` rather than becoming a MISMATCH line that
    blames the artifact for a bug in the compiler."""
    module = _py_emit_module()

    def boom(_ir):
        raise ValueError("an internal emitter fault, not a refusal")

    monkeypatch.setattr(module, "emit", boom)
    _record_py_artifact(registry, "00" * 32)
    with pytest.raises(ValueError, match="an internal emitter fault"):
        R.reproduce(COMPONENT, registry=str(registry))


def test_a_subclass_of_the_refusal_class_is_still_a_refusal(registry, monkeypatch):
    """A tier that refines its own `EmitError` is still refusing, so the catch
    is by class and not by identity."""
    module = _py_emit_module()

    class Narrower(module.EmitError):
        pass

    def refuse(_ir):
        raise Narrower("this tier will not lower that shape")

    monkeypatch.setattr(module, "emit", refuse)
    ir = compile_files([str(_entry(registry) / "component.rvl")])
    emitted, refusal = R._emit_backend_source("python", ir)
    assert emitted is None
    assert refusal == "this tier will not lower that shape"


def test_an_absent_emitter_is_still_cannot_verify(registry):
    """The `ImportError` leg, unchanged: a backend that is not installed here
    decided nothing about this IR, so the tier degrades honestly instead of
    becoming a MISMATCH."""
    _entry(registry).joinpath("artifacts.json").write_text(json.dumps(
        {"backends": {"no_such_backend": {"sourceSha256": "00" * 32}}}))
    report = R.reproduce(COMPONENT, registry=str(registry))
    art = _tier(report, f"{R.TIER_ARTIFACT} [no_such_backend]")
    assert art is not None and art.status == R.UNVERIFIED
    assert "not available here" in art.detail
    assert report.ok, "an absent toolchain is not a divergence"


def test_the_refusal_class_is_read_off_the_module_object(registry):
    """Why `_refusal_class` exists at all. `EmitError` is defined INSIDE each
    backend's `emit.py`; there is no class under `src/` to name in an `except`
    clause, and two loads of the same file produce two unrelated classes, so an
    `except` against one does not catch an instance of the other. The class has
    to come off the module that will raise."""
    import types  # noqa: PLC0415

    first = types.ModuleType("fake_emit_one")
    second = types.ModuleType("fake_emit_two")
    first.EmitError = type("EmitError", (ValueError,), {})
    second.EmitError = type("EmitError", (ValueError,), {})

    assert R._refusal_class(first) is first.EmitError
    assert R._refusal_class(second) is second.EmitError
    assert first.EmitError is not second.EmitError
    assert not isinstance(second.EmitError("x"), first.EmitError)


def test_a_module_with_no_refusal_class_treats_everything_as_a_fault(registry, monkeypatch):
    """CONTROL for the helper. A backend that declares no `EmitError` has no
    refusal vocabulary, so `_refusal_class` returns None and the catch tuple is
    empty: everything that emitter raises is a fault and keeps its traceback.
    Nothing is caught by accident on the way to an empty tuple."""
    import types  # noqa: PLC0415

    module = _py_emit_module()
    assert R._refusal_class(types.ModuleType("no_emit_error")) is None
    # a non-class, and a class that is not an exception, are both "no refusal
    # vocabulary" rather than a TypeError out of `except`.
    weird = types.ModuleType("weird")
    weird.EmitError = "not a class"
    assert R._refusal_class(weird) is None
    weird.EmitError = dict
    assert R._refusal_class(weird) is None

    refusal_class = module.EmitError

    def refuse(_ir):
        raise refusal_class("a refusal from a module that declares none")

    monkeypatch.delattr(module, "EmitError")
    monkeypatch.setattr(module, "emit", refuse)
    ir = compile_files([str(_entry(registry) / "component.rvl")])
    with pytest.raises(ValueError, match="a refusal from a module that declares none"):
        R._emit_backend_source("python", ir)

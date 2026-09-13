"""Real TEE quote formats and the attestation root (roadmap item 475, issue #827).

`tee_attestation` shipped with one signature algorithm, a symmetric MAC, which
proves that the holder of a key authored a record. That is not an attestation
root, and the module said so. `tee_quote` is the root: the two quote formats a
confidential worker actually produces, parsed at the offsets the vendors define,
verified with real ECDSA, chained to a public key the OPERATOR pinned.

This file is mostly refusals, because the refusals are the property. The
committed fixtures in `tests/fixtures/tee/` each record the verdict they must
reach in `manifest.json`, and the parametrised test below reads those rather than
restating them, so adding a fixture adds a test. The forged cases are RE-SIGNED
rather than corrupted: `tdx_quote_other_platform.bin` is an internally consistent
quote from a platform the root does not endorse, which is the case that matters,
and a test that only flipped a signature byte would not show it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import tee_quote as q  # noqa: E402
from revl.tee_quote import (  # noqa: E402
    CURVE_P256,
    CURVE_P384,
    DevMacRoot,
    HardwareRoot,
    PinnedKey,
    PlatformEndorsement,
    QuoteFormatError,
    RootError,
    derive_public_key,
    ecdsa_sign,
    ecdsa_verify,
    parse_sev_snp_report,
    parse_tdx_quote,
    private_key_from_seed,
    public_key_id,
    require_production_root,
    sign_endorsement,
    verify_quote,
)

FIXTURES = ROOT / "tests" / "fixtures" / "tee"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _generator():
    """The committed generator, imported as a module so the fixtures can be
    re-derived and compared byte for byte."""
    spec = importlib.util.spec_from_file_location(
        "tee_fixture_generator", FIXTURES / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pinned(name: str) -> PinnedKey:
    entry = MANIFEST["keys"][name]
    return PinnedKey(entry["curve"], bytes.fromhex(entry["public_key"]), name)


def _key_id(name: str) -> str:
    return MANIFEST["keys"][name]["key_id"]


def _root(**overrides) -> HardwareRoot:
    """A root that pins the two platform keys the good fixtures were signed with,
    and the AMD root key the endorsements were signed with."""
    fields = dict(
        platform_keys=[_pinned("tdx_platform"), _pinned("sev_vcek")],
        endorsement_roots=[_pinned("amd_root")])
    fields.update(overrides)
    return HardwareRoot(**fields)


def _verify(entry: dict, **overrides):
    blob = (FIXTURES / entry["file"]).read_bytes()
    fields = dict(
        sign_alg=entry["format"],
        root=_root(),
        expect_report_data=bytes.fromhex(MANIFEST["report_data"]),
        platform_key_id=(_key_id("tdx_platform")
                         if entry["format"] == q.SIGN_ALG_TDX
                         else _key_id("sev_vcek")),
        now=NOW)
    fields.update(overrides)
    return verify_quote(blob, **fields)


# ------------------------------------------------------- the curve parameters


@pytest.mark.parametrize("curve", [CURVE_P256, CURVE_P384], ids=lambda c: c.name)
def test_the_curve_parameters_are_self_consistent(curve):
    """A mistyped constant would verify nothing while looking like it verified
    everything, so the parameters are re-derived here rather than trusted: the
    generator must lie on the curve, and its order must annihilate it."""
    assert q._on_curve(curve, (curve.gx, curve.gy))
    assert q._mul(curve, curve.n, (curve.gx, curve.gy)) is None
    assert curve.p.bit_length() == curve.n.bit_length()


def test_each_prime_is_the_formula_the_standard_defines():
    """Written as the formula in the source, checked against the transcription
    here: only one of the two can be wrong without this reddening."""
    assert CURVE_P256.p == 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
    assert CURVE_P384.p == 2**384 - 2**128 - 2**96 + 2**32 - 1
    assert CURVE_P256.coord_len == 32 and CURVE_P384.coord_len == 48


# --------------------------------------------------------------------- ECDSA


@pytest.mark.parametrize("curve", [CURVE_P256, CURVE_P384], ids=lambda c: c.name)
def test_a_signature_verifies_and_is_deterministic(curve):
    scalar = private_key_from_seed(curve, b"round-trip")
    public = derive_public_key(curve, scalar)
    first = ecdsa_sign(curve, scalar, b"a message")
    assert first == ecdsa_sign(curve, scalar, b"a message")  # RFC 6979
    assert ecdsa_verify(curve, public, b"a message", first)
    assert not ecdsa_verify(curve, public, b"another message", first)


@pytest.mark.parametrize("curve", [CURVE_P256, CURVE_P384], ids=lambda c: c.name)
def test_a_signature_under_another_key_is_refused(curve):
    scalar = private_key_from_seed(curve, b"signer")
    other = derive_public_key(curve, private_key_from_seed(curve, b"someone else"))
    assert not ecdsa_verify(curve, other, b"m", ecdsa_sign(curve, scalar, b"m"))


@pytest.mark.parametrize("curve", [CURVE_P256, CURVE_P384], ids=lambda c: c.name)
def test_a_non_canonical_scalar_is_refused(curve):
    """The classic admission: a signature of all zeroes, or one whose scalars sit
    at or above the group order, must not verify."""
    scalar = private_key_from_seed(curve, b"signer")
    public = derive_public_key(curve, scalar)
    width = curve.order_len
    zero = (0).to_bytes(width, "big")
    one = (1).to_bytes(width, "big")
    order = curve.n.to_bytes(width, "big")
    assert not ecdsa_verify(curve, public, b"m", zero + zero)
    assert not ecdsa_verify(curve, public, b"m", zero + one)
    assert not ecdsa_verify(curve, public, b"m", one + zero)
    assert not ecdsa_verify(curve, public, b"m", order + one)
    assert not ecdsa_verify(curve, public, b"m", one + order)
    assert not ecdsa_verify(curve, public, b"m", b"")


@pytest.mark.parametrize("curve", [CURVE_P256, CURVE_P384], ids=lambda c: c.name)
def test_a_public_key_off_the_curve_is_refused_where_it_is_configured(curve):
    """An off-curve key is the VERIFIER's own mistake, not a peer's record, so it
    raises where it can be fixed instead of quietly deciding nothing."""
    width = curve.coord_len
    bogus = (1).to_bytes(width, "big") + (1).to_bytes(width, "big")
    with pytest.raises(QuoteFormatError, match="not a point on the curve"):
        ecdsa_verify(curve, bogus, b"m", b"\x01" * (2 * curve.order_len))
    with pytest.raises(QuoteFormatError, match="point at infinity"):
        ecdsa_verify(curve, b"\x00" * (2 * width), b"m", b"\x01" * (2 * curve.order_len))
    with pytest.raises(RootError, match="unusable"):
        PinnedKey(curve.name, bogus)


def test_an_uncompressed_sec1_prefix_is_accepted_on_input():
    scalar = private_key_from_seed(CURVE_P256, b"sec1")
    public = derive_public_key(CURVE_P256, scalar)
    assert ecdsa_verify(CURVE_P256, b"\x04" + public, b"m",
                        ecdsa_sign(CURVE_P256, scalar, b"m"))


# ------------------------------------------------------ the committed fixtures


def test_the_committed_fixtures_are_reproducible_from_the_generator():
    """The bytes in `tests/fixtures/tee/` are a pure function of the generator, and
    this is what makes them reviewable: a builder change that alters the wire
    layout reddens here instead of silently testing a format nothing emits."""
    generator = _generator()
    keys = generator.scalars()
    produced = dict((name, blob) for name, blob, _ in
                    generator.tdx_fixtures(keys) + generator.sev_fixtures(keys))
    assert produced, "the generator produced no fixtures"
    for name, blob in produced.items():
        assert (FIXTURES / name).read_bytes() == blob, name
    recorded = {entry["file"] for entry in MANIFEST["fixtures"]}
    assert recorded == set(produced), "manifest.json and the generator disagree"


@pytest.mark.parametrize("entry", MANIFEST["fixtures"], ids=lambda e: e["file"])
def test_each_committed_fixture_reaches_its_recorded_verdict(entry):
    """One test per fixture, expectation read from the manifest beside it. The
    refusals are the point: every malformed, forged, re-purposed and debuggable
    quote is refused by name, and nothing is admitted that the pinned root did
    not vouch for."""
    verdict = _verify(entry)
    if entry["verdict"] == "admit":
        assert verdict.ok, verdict.reason
        assert verdict.measurement == entry["measurement"]
        assert verdict.platform_key_id
        assert not verdict.debug_enabled
    else:
        assert not verdict.ok
        assert entry["expect"] in verdict.reason, verdict.reason
        assert verdict.measurement == "", (
            "a refused quote must report no measurement, or a caller comparing it "
            "against a permitted set could find a refused quote acceptable")


@pytest.mark.parametrize("entry", [e for e in MANIFEST["fixtures"]
                                   if e["verdict"] == "admit"],
                         ids=lambda e: e["file"])
def test_an_admitted_fixture_is_refused_by_a_root_that_pins_another_platform(entry):
    """The forged-root direction from the other side: the same good quote, against
    a root that pins a different platform key, refuses."""
    other = PinnedKey(MANIFEST["keys"]["tdx_other_platform"]["curve"],
                      bytes.fromhex(MANIFEST["keys"]["tdx_other_platform"]["public_key"]))
    sev_other = PinnedKey(MANIFEST["keys"]["sev_other_vcek"]["curve"],
                          bytes.fromhex(MANIFEST["keys"]["sev_other_vcek"]["public_key"]))
    verdict = _verify(entry, root=HardwareRoot(platform_keys=[other, sev_other]))
    assert not verdict.ok
    assert "no attestation root holds a platform key" in verdict.reason


# --------------------------------------------------------------- the parsers


def test_a_tdx_quote_round_trips_through_the_parser():
    quote = parse_tdx_quote((FIXTURES / "tdx_quote_good.bin").read_bytes())
    assert quote.version == q.TDX_QUOTE_VERSION
    assert quote.tee_type == q.TDX_TEE_TYPE
    assert quote.qe_vendor_id == q.INTEL_QE_VENDOR_ID
    assert quote.mrtd.hex() == MANIFEST["fixtures"][0]["measurement"]
    assert len(quote.mrtd) == 48 and len(quote.report_data) == 64
    assert len(quote.rtmrs) == 4 and all(len(r) == 48 for r in quote.rtmrs)
    assert len(quote.qe_report) == 384
    assert quote.report_data.hex() == MANIFEST["report_data"]
    assert len(quote.signed_bytes) == 48 + 584


def test_a_sev_snp_report_round_trips_through_the_parser():
    report = parse_sev_snp_report((FIXTURES / "sev_snp_report_good.bin").read_bytes())
    assert report.version in q.SEV_SNP_VERSIONS
    assert report.signature_algo == q.SEV_SNP_SIG_ALGO_ECDSA_P384
    assert len(report.measurement) == 48 and len(report.report_data) == 64
    assert len(report.chip_id) == 64
    assert report.report_data.hex() == MANIFEST["report_data"]
    assert len(report.signed_bytes) == q.SEV_SNP_SIGNED_LEN
    assert len(report.signature) == 2 * CURVE_P384.order_len
    assert not report.debug_enabled


@pytest.mark.parametrize("blob", [None, "not bytes", 17, []])
def test_a_quote_that_is_not_bytes_is_refused_rather_than_read(blob):
    with pytest.raises(QuoteFormatError):
        parse_tdx_quote(blob)
    with pytest.raises(QuoteFormatError):
        parse_sev_snp_report(blob)


def test_the_amd_signature_field_survives_a_round_trip():
    """R and S are little-endian in 72-byte slots, which is the detail a reader
    written for the big-endian convention gets silently wrong."""
    raw = ecdsa_sign(CURVE_P384, private_key_from_seed(CURVE_P384, b"vcek"), b"m")
    assert q._sev_decode_signature(q._sev_encode_signature(raw)) == raw


# ----------------------------------------------------------------- the root


def test_a_root_that_can_name_no_key_is_refused_where_it_is_built():
    with pytest.raises(RootError, match="decides nothing"):
        HardwareRoot()


def test_a_root_configured_with_a_bare_key_is_refused():
    """The curve is part of the pin, so the operator's configuration cannot be
    reinterpreted on a curve of the peer's choosing."""
    with pytest.raises(RootError, match="PinnedKey"):
        HardwareRoot(platform_keys=[bytes.fromhex(
            MANIFEST["keys"]["sev_vcek"]["public_key"])])


def test_a_platform_key_pinned_on_the_wrong_curve_is_refused():
    verdict = _verify(
        MANIFEST["fixtures"][0],
        root=HardwareRoot(platform_keys=[
            PinnedKey("P-384", bytes.fromhex(
                MANIFEST["keys"]["sev_vcek"]["public_key"]), "mislabelled")]),
        platform_key_id=_key_id("sev_vcek"))
    assert not verdict.ok
    assert "signs on P-256" in verdict.reason


def test_a_quote_with_no_platform_fingerprint_is_refused():
    for fingerprint in (None, "", 5, "not-a-fingerprint"):
        verdict = _verify(MANIFEST["fixtures"][0], platform_key_id=fingerprint)
        assert not verdict.ok, fingerprint
        assert ("names no platform key fingerprint" in verdict.reason
                or "no attestation root holds a platform key" in verdict.reason)


def test_the_expected_report_data_must_be_the_full_challenge_width():
    for width in (0, 32, 63, 65):
        verdict = _verify(MANIFEST["fixtures"][0],
                          expect_report_data=b"\x00" * width)
        assert not verdict.ok
        assert "exactly 64 bytes" in verdict.reason


def test_an_unknown_quote_format_is_refused():
    verdict = _verify(MANIFEST["fixtures"][0], sign_alg="hmac-sha256")
    assert not verdict.ok and "unknown quote format" in verdict.reason


# ------------------------------------------------ the development verifier, fenced


def test_the_symmetric_verifier_cannot_be_built_without_saying_so():
    with pytest.raises(RootError, match="development and test verifier"):
        DevMacRoot(b"a shared secret")
    dev = DevMacRoot(b"a shared secret", acknowledged_dev_only=True)
    assert dev.is_production is False
    assert "NOT an attestation root" in dev.describe()


def test_the_symmetric_verifier_is_refused_as_a_production_root():
    dev = DevMacRoot(b"k", acknowledged_dev_only=True)
    with pytest.raises(RootError, match="not a production attestation root"):
        require_production_root(dev)
    assert require_production_root(_root()) is not None
    with pytest.raises(RootError):
        require_production_root(None)


def test_the_symmetric_verifier_cannot_decide_a_hardware_quote():
    verdict = _verify(MANIFEST["fixtures"][0],
                      root=DevMacRoot(b"k", acknowledged_dev_only=True))
    assert not verdict.ok
    assert "needs a hardware attestation root" in verdict.reason


# ----------------------------------------------------------- the endorsement hop


def _sev_entry() -> dict:
    return next(e for e in MANIFEST["fixtures"] if e["file"] == "sev_snp_report_good.bin")


def test_a_platform_key_a_pinned_root_endorses_is_accepted():
    """An operator may pin one vendor root and let it speak for platform keys it
    has never seen, which is the shape the vendor's own chain has."""
    verdict = _verify(_sev_entry(),
                      root=HardwareRoot(endorsement_roots=[_pinned("amd_root")]),
                      endorsement=MANIFEST["endorsements"]["good"])
    assert verdict.ok, verdict.reason
    assert verdict.platform_key_id == _key_id("sev_vcek")


@pytest.mark.parametrize("case,expect", [
    ("expired", "expired at"),
    ("signed_by_an_unpinned_root", "which this verifier has not pinned"),
    ("window_extended_after_signing", "does not verify under pinned root"),
])
def test_an_endorsement_that_does_not_reach_the_pinned_root_is_refused(case, expect):
    verdict = _verify(_sev_entry(),
                      root=HardwareRoot(endorsement_roots=[_pinned("amd_root")]),
                      endorsement=MANIFEST["endorsements"][case])
    assert not verdict.ok
    assert expect in verdict.reason, verdict.reason


def test_an_endorsement_for_a_different_platform_key_is_refused():
    """The endorsement must certify the key the evidence names, or the hop proves
    something about a key nobody asked about."""
    verdict = _verify(_sev_entry(),
                      root=HardwareRoot(endorsement_roots=[_pinned("amd_root")]),
                      endorsement=MANIFEST["endorsements"]["good"],
                      platform_key_id=_key_id("sev_other_vcek"))
    assert not verdict.ok
    assert "but the evidence names" in verdict.reason


def test_an_endorsement_for_another_vendor_is_refused():
    forged = dict(MANIFEST["endorsements"]["good"])
    forged["vendor"] = q.VENDOR_INTEL_TDX
    verdict = _verify(_sev_entry(),
                      root=HardwareRoot(endorsement_roots=[_pinned("amd_root")]),
                      endorsement=forged)
    assert not verdict.ok and "is for vendor" in verdict.reason


@pytest.mark.parametrize("endorsement", [None, "a string", 7, []])
def test_a_malformed_endorsement_is_refused_rather_than_read(endorsement):
    verdict = _verify(_sev_entry(),
                      root=HardwareRoot(endorsement_roots=[_pinned("amd_root")]),
                      endorsement=endorsement)
    assert not verdict.ok


def test_an_endorsement_outside_its_own_window_is_refused_when_it_is_built():
    with pytest.raises(RootError, match="never valid"):
        PlatformEndorsement(vendor=q.VENDOR_AMD_SEV_SNP, curve="P-384",
                            platform_key=MANIFEST["keys"]["sev_vcek"]["public_key"],
                            not_before="2026-06-01T00:00:00+00:00",
                            not_after="2026-01-01T00:00:00+00:00")


def test_signing_an_endorsement_needs_the_pinned_root_s_own_scalar():
    pin = _pinned("amd_root")
    wrong = private_key_from_seed(CURVE_P384, b"not the root")
    endorsement = PlatformEndorsement(
        vendor=q.VENDOR_AMD_SEV_SNP, curve="P-384",
        platform_key=MANIFEST["keys"]["sev_vcek"]["public_key"],
        not_before="2026-01-01T00:00:00+00:00",
        not_after="2027-01-01T00:00:00+00:00")
    with pytest.raises(RootError, match="does not derive the pinned root"):
        sign_endorsement(endorsement, pin, wrong)


def test_a_key_fingerprint_is_the_same_construction_the_tree_already_uses():
    from revl.attest import key_id
    public = bytes.fromhex(MANIFEST["keys"]["sev_vcek"]["public_key"])
    assert public_key_id(public) == key_id(public) == _key_id("sev_vcek")

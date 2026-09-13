"""Regenerate the committed TEE quote fixtures (roadmap item 475, issue #827).

    python tests/fixtures/tee/generate.py

Every byte in this directory is a pure function of this script. The keys come
from `tee_quote.private_key_from_seed`, and the signatures are deterministic
(RFC 6979), so re-running it on any machine and any supported Python reproduces
the committed files exactly. `tests/test_tee_quote.py` asserts that: a fixture
whose bytes drift from what the builders produce reddens rather than quietly
testing a format the code no longer emits.

These are not Intel- or AMD-issued quotes. Nobody can commit one of those and
have it stay verifiable: a genuine quote is signed by a per-platform key whose
certificate chain terminates in a live vendor service, and the private half is
inside hardware this repository does not have. What IS committed is the part a
verifier can be held to: the real wire LAYOUTS, at the real offsets, with real
ECDSA over the curves the two vendors specify, chained to a root key pinned the
way an operator pins one. The refusal each malformed or forged fixture must
produce is recorded beside it in `manifest.json`, so the test file reads the
expectations rather than restating them.
"""

from __future__ import annotations

import json
import pathlib
import struct
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "src"))

from revl import tee_quote as q  # noqa: E402

# Seeds, not keys. The scalars are `private_key_from_seed` of these, so the
# fixtures are reproducible and no key material is invented by hand.
SEEDS = {
    "tdx_attest": b"revl-fixture/tdx-attestation-key",
    "tdx_platform": b"revl-fixture/tdx-pck-key",
    "tdx_other_platform": b"revl-fixture/tdx-pck-key-of-another-platform",
    "sev_vcek": b"revl-fixture/sev-snp-vcek",
    "sev_other_vcek": b"revl-fixture/sev-snp-vcek-of-another-platform",
    "amd_root": b"revl-fixture/amd-root-key",
    "rogue_root": b"revl-fixture/a-root-the-operator-never-pinned",
}

REPORT_DATA = bytes(range(64))
OTHER_REPORT_DATA = bytes(range(64, 128))
MRTD = bytes.fromhex("a1" * 48)
OTHER_MRTD = bytes.fromhex("b2" * 48)
SEV_MEASUREMENT = bytes.fromhex("c3" * 48)
OTHER_SEV_MEASUREMENT = bytes.fromhex("d4" * 48)
SEV_POLICY = 0x30000
SEV_POLICY_DEBUG = SEV_POLICY | (1 << q.SEV_SNP_POLICY_DEBUG_BIT)

WINDOW = ("2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00")
EXPIRED_WINDOW = ("2025-01-01T00:00:00+00:00", "2025-06-01T00:00:00+00:00")


def scalars() -> dict:
    out = {}
    for name, seed in SEEDS.items():
        curve = q.CURVE_P256 if name.startswith("tdx") else q.CURVE_P384
        out[name] = (curve, q.private_key_from_seed(curve, seed))
    return out


def tdx_fixtures(keys: dict) -> list[tuple[str, bytes, dict]]:
    _, ak = keys["tdx_attest"]
    _, pck = keys["tdx_platform"]
    _, other_pck = keys["tdx_other_platform"]
    common = dict(attest_private_key=ak, platform_private_key=pck)
    good = q.build_tdx_quote(report_data=REPORT_DATA, mrtd=MRTD, **common)

    tampered = bytearray(good)
    tampered[48 + 136:48 + 184] = OTHER_MRTD  # the MRTD field, left unsigned

    oversize = bytearray(good)
    struct.pack_into("<I", oversize, 48 + 584,
                     struct.unpack_from("<I", oversize, 48 + 584)[0] + 4)

    return [
        ("tdx_quote_good.bin", good, {
            "verdict": "admit", "measurement": MRTD.hex(),
            "why": "the reference quote every other TDX fixture is one change away from"}),
        ("tdx_quote_other_measurement.bin",
         q.build_tdx_quote(report_data=REPORT_DATA, mrtd=OTHER_MRTD, **common), {
             "verdict": "admit", "measurement": OTHER_MRTD.hex(),
             "why": "verifies, and reports a measurement a requirement need not permit: "
                    "the wrong-measurement refusal belongs to the placement, not the root"}),
        ("tdx_quote_other_platform.bin",
         q.build_tdx_quote(report_data=REPORT_DATA, mrtd=MRTD,
                           attest_private_key=ak, platform_private_key=other_pck), {
             "verdict": "refuse", "expect": "not signed by the platform key pinned as",
             "why": "a complete, internally consistent quote from a platform this root "
                    "does not endorse: the forged-root case, re-signed rather than corrupted"}),
        ("tdx_quote_unattached_attestation_key.bin",
         q.build_tdx_quote(report_data=REPORT_DATA, mrtd=MRTD,
                           qe_report_data=b"\x00" * 64, **common), {
             "verdict": "refuse", "expect": "does not bind this attestation key",
             "why": "the platform signs a QE Report that certifies no key, so the "
                    "attestation key is the peer's to choose"}),
        ("tdx_quote_tampered_mrtd.bin", bytes(tampered), {
            "verdict": "refuse", "expect": "not signed by the attestation key",
            "why": "the measurement register altered after the quote was signed"}),
        ("tdx_quote_wrong_challenge.bin",
         q.build_tdx_quote(report_data=OTHER_REPORT_DATA, mrtd=MRTD, **common), {
             "verdict": "refuse", "expect": "report_data does not match",
             "why": "a valid quote that answers a different challenge: the replay case"}),
        ("tdx_quote_debug_td.bin",
         q.build_tdx_quote(report_data=REPORT_DATA, mrtd=MRTD,
                           td_attributes=b"\x01" + b"\x00" * 7, **common), {
             "verdict": "refuse", "expect": "TDATTRIBUTES debug bit is set",
             "why": "a debuggable TD is inspectable from its host"}),
        ("tdx_quote_other_qe_vendor.bin",
         q.build_tdx_quote(report_data=REPORT_DATA, mrtd=MRTD,
                           qe_vendor_id=bytes.fromhex("00" * 16), **common), {
             "verdict": "refuse", "expect": "QE vendor id",
             "why": "signed correctly, and by a quoting enclave that is not Intel's"}),
        ("tdx_quote_truncated.bin", good[:600], {
            "verdict": "refuse", "expect": "malformed TDX quote",
            "why": "shorter than a header plus a body plus a length"}),
        ("tdx_quote_version_3.bin",
         q.build_tdx_quote(report_data=REPORT_DATA, mrtd=MRTD, version=3, **common), {
             "verdict": "refuse", "expect": "unsupported TDX quote version 3",
             "why": "an older layout refused rather than read with this one"}),
        ("tdx_quote_sgx_tee_type.bin",
         q.build_tdx_quote(report_data=REPORT_DATA, mrtd=MRTD,
                           tee_type=q.SGX_TEE_TYPE, **common), {
             "verdict": "refuse", "expect": "an SGX quote, whose body is a different structure",
             "why": "an SGX report body is not a TD quote body"}),
        ("tdx_quote_trailing_bytes.bin", good + b"\x00" * 4, {
            "verdict": "refuse", "expect": "bytes follow it",
            "why": "four unsigned bytes appended; the declared signature length no "
                   "longer accounts for what is present"}),
        ("tdx_quote_signature_length_overrun.bin", bytes(oversize), {
            "verdict": "refuse", "expect": "signature_data_len",
            "why": "the declared length claims bytes the quote does not carry"}),
    ]


def sev_fixtures(keys: dict) -> list[tuple[str, bytes, dict]]:
    _, vcek = keys["sev_vcek"]
    _, other_vcek = keys["sev_other_vcek"]
    good = q.build_sev_snp_report(report_data=REPORT_DATA,
                                 measurement=SEV_MEASUREMENT,
                                 vcek_private_key=vcek, policy=SEV_POLICY)
    tampered = bytearray(good)
    tampered[0x090:0x0C0] = OTHER_SEV_MEASUREMENT
    oversized = bytearray(good)
    oversized[0x2A0 + q.CURVE_P384.order_len] = 0x01  # a byte above a P-384 scalar

    return [
        ("sev_snp_report_good.bin", good, {
            "verdict": "admit", "measurement": SEV_MEASUREMENT.hex(),
            "why": "the reference report every other SEV-SNP fixture is one change from"}),
        ("sev_snp_report_other_measurement.bin",
         q.build_sev_snp_report(report_data=REPORT_DATA,
                                measurement=OTHER_SEV_MEASUREMENT,
                                vcek_private_key=vcek, policy=SEV_POLICY), {
             "verdict": "admit", "measurement": OTHER_SEV_MEASUREMENT.hex(),
             "why": "verifies, and reports a measurement a requirement need not permit"}),
        ("sev_snp_report_other_vcek.bin",
         q.build_sev_snp_report(report_data=REPORT_DATA, measurement=SEV_MEASUREMENT,
                                vcek_private_key=other_vcek, policy=SEV_POLICY), {
             "verdict": "refuse", "expect": "not signed by the VCEK pinned as",
             "why": "a complete report from a platform this root does not endorse"}),
        ("sev_snp_report_tampered_measurement.bin", bytes(tampered), {
            "verdict": "refuse", "expect": "not signed by the VCEK pinned as",
            "why": "the launch measurement altered after the report was signed"}),
        ("sev_snp_report_wrong_challenge.bin",
         q.build_sev_snp_report(report_data=OTHER_REPORT_DATA,
                                measurement=SEV_MEASUREMENT,
                                vcek_private_key=vcek, policy=SEV_POLICY), {
             "verdict": "refuse", "expect": "report_data does not match",
             "why": "a valid report that answers a different challenge"}),
        ("sev_snp_report_debug_policy.bin",
         q.build_sev_snp_report(report_data=REPORT_DATA, measurement=SEV_MEASUREMENT,
                                vcek_private_key=vcek, policy=SEV_POLICY_DEBUG), {
             "verdict": "refuse", "expect": "allows debug",
             "why": "a debuggable guest's memory is readable by its host"}),
        ("sev_snp_report_short.bin", good[:-1], {
            "verdict": "refuse", "expect": "exactly 1184 bytes",
            "why": "the report is a fixed size and a short one is refused, not padded"}),
        ("sev_snp_report_version_9.bin",
         q.build_sev_snp_report(report_data=REPORT_DATA, measurement=SEV_MEASUREMENT,
                                vcek_private_key=vcek, policy=SEV_POLICY, version=9), {
             "verdict": "refuse", "expect": "unsupported SEV-SNP report version 9",
             "why": "an unknown layout refused rather than read with this one"}),
        ("sev_snp_report_bad_sig_algo.bin",
         q.build_sev_snp_report(report_data=REPORT_DATA, measurement=SEV_MEASUREMENT,
                                vcek_private_key=vcek, policy=SEV_POLICY,
                                signature_algo=2), {
             "verdict": "refuse", "expect": "signature_algo is 2",
             "why": "an algorithm the format does not define"}),
        ("sev_snp_report_oversized_scalar.bin", bytes(oversized), {
            "verdict": "refuse", "expect": "bytes above the 48",
            "why": "a signature scalar that spills past P-384, which a truncating "
                   "reader would silently accept as a different scalar"}),
    ]


def endorsements(keys: dict) -> dict:
    _, amd_root = keys["amd_root"]
    _, rogue = keys["rogue_root"]
    _, vcek = keys["sev_vcek"]
    vcek_pub = q.derive_public_key(q.CURVE_P384, vcek)
    root_pin = q.PinnedKey("P-384", q.derive_public_key(q.CURVE_P384, amd_root), "amd-root")
    rogue_pin = q.PinnedKey("P-384", q.derive_public_key(q.CURVE_P384, rogue), "rogue")

    def endorse(window, root_pin_, root_scalar):
        return q.sign_endorsement(
            q.PlatformEndorsement(vendor=q.VENDOR_AMD_SEV_SNP, curve="P-384",
                                  platform_key=vcek_pub.hex(),
                                  not_before=window[0], not_after=window[1]),
            root_pin_, root_scalar)

    good = endorse(WINDOW, root_pin, amd_root)
    expired = endorse(EXPIRED_WINDOW, root_pin, amd_root)
    rogue_signed = endorse(WINDOW, rogue_pin, rogue)
    altered = dict(good)
    altered["not_after"] = "2099-01-01T00:00:00+00:00"
    return {
        "good": good,
        "expired": expired,
        "signed_by_an_unpinned_root": rogue_signed,
        "window_extended_after_signing": altered,
    }


def main() -> None:
    keys = scalars()
    written = []
    for name, blob, meta in tdx_fixtures(keys) + sev_fixtures(keys):
        (HERE / name).write_bytes(blob)
        fmt = q.SIGN_ALG_TDX if name.startswith("tdx") else q.SIGN_ALG_SEV_SNP
        written.append(dict(file=name, format=fmt, bytes=len(blob), **meta))

    pinned = {}
    for name, (curve, scalar) in sorted(keys.items()):
        pub = q.derive_public_key(curve, scalar)
        pinned[name] = {"curve": curve.name, "public_key": pub.hex(),
                        "key_id": q.public_key_id(pub)}

    manifest = {
        "note": ("Committed TEE quote fixtures for roadmap item 475. Regenerate with "
                 "`python tests/fixtures/tee/generate.py`; the bytes are a pure "
                 "function of this directory's generator and are asserted to be. "
                 "These are not vendor-issued quotes (see the generator's docstring) "
                 "- they are the real wire layouts, signed with the real curves, "
                 "chained to a pinned root."),
        "seeds": {k: v.decode() for k, v in sorted(SEEDS.items())},
        "keys": pinned,
        "report_data": REPORT_DATA.hex(),
        "other_report_data": OTHER_REPORT_DATA.hex(),
        "endorsements": endorsements(keys),
        "fixtures": written,
    }
    (HERE / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(written)} fixtures and manifest.json")


if __name__ == "__main__":
    main()

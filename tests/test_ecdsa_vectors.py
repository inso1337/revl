"""`src/revl/tee_quote.py`'s ECDSA against vectors somebody else wrote.

The module implements ECDSA over NIST P-256 and P-384 in pure Python integers,
and it is what decides whether a TEE attestation quote is genuine. Its own test
file checks it against itself: it signs with `ecdsa_sign` and verifies with
`ecdsa_verify`, which is a round trip, and a round trip is satisfied by any
self-consistent pair of functions including a pair that agrees on the wrong
answer. This file checks it against authority instead.

Two sources, both committed:

  * Project Wycheproof's IEEE P1363 ECDSA verification suites, which are built
    specifically out of the mistakes hand-rolled ECDSA makes: r=0 and s=0
    admission (CVE-2022-21449), r or s at or past the group order, an
    intermediate point at infinity in the two-scalar multiplication, modular
    inverse edge cases, and truncated or over-long signature encodings.
  * RFC 6979 Appendix A.2.5 and A.2.6, which pin the deterministic nonce `k`
    itself rather than only the signature it produces.

Both live under `tests/fixtures/crypto/`; see the PROVENANCE.md there. Nothing
here reaches the network and nothing here skips. That is deliberate: a gate that
needs a download runs green on the day the download fails, because a skip and a
pass report the same colour (issue #266, roadmap item 445).

What this file does NOT cover, stated so nobody reads more into a green run:
timing. The module says in its own docstring that `_mul` is not constant time
and argues every scalar it multiplies by is public. That argument is about the
call sites, not about the arithmetic, and no test here examines it.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from revl.tee_quote import (
    CURVE_P256,
    CURVE_P384,
    Curve,
    ecdsa_sign,
    ecdsa_verify,
    derive_public_key,
    _bits2int,
    _rfc6979_k,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "crypto"
WYCHEPROOF = FIXTURES / "wycheproof"

#: Wycheproof names curves the way SEC does; `tee_quote` names them the way
#: FIPS 186-4 does. Same curves.
BY_SEC_NAME = {"secp256r1": CURVE_P256, "secp384r1": CURVE_P384}

#: Each suite, and the number of cases it held when it was committed. The count
#: is asserted, so a fixture that is truncated, emptied or quietly replaced by a
#: smaller one reddens instead of reporting a green run over nothing.
SUITES = (
    ("ecdsa_secp256r1_sha256_p1363_test.json", 262),
    ("ecdsa_secp256r1_sha512_p1363_test.json", 332),
    ("ecdsa_secp384r1_sha384_p1363_test.json", 280),
    ("ecdsa_secp384r1_sha512_p1363_test.json", 318),
)

TOTAL_WYCHEPROOF_CASES = sum(count for _, count in SUITES)

#: The bug families the suites are carried FOR. Wycheproof tags every case with
#: the flaw it was constructed to expose; if a future refresh of these fixtures
#: drops one of these, the coverage is gone and this list says so out loud
#: rather than letting the pass count quietly stand in for it.
REQUIRED_FLAGS = frozenset({
    "InvalidSignature",             # r=0 / s=0, CVE-2022-21449
    "RangeCheck",                   # r or s at or beyond the group order
    "PointDuplication",             # identity and duplication in the group law
    "EdgeCaseShamirMultiplication",  # intermediate point at infinity
    "ModularInverse",
    "ArithmeticError",
    "SignatureSize",
    "SmallRandS",
    "SpecialCaseHash",
    "EdgeCasePublicKey",
    "IntegerOverflow",
    "ValidSignature",
})


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _curve_for(group: dict) -> Curve:
    """The `Curve` a Wycheproof group describes, with the group's own hash.

    `tee_quote` binds one hash to each curve because each quote format does.
    The suites pair P-256 and P-384 with a second, wider hash as well, and that
    pairing is the point: it exercises `_bits2int`'s truncation, which the
    matched-hash pairing never reaches."""
    base = BY_SEC_NAME[group["publicKey"]["curve"]]
    digest = group["sha"].replace("-", "").lower()
    return dataclasses.replace(base, digest=digest)


@pytest.mark.parametrize("filename,expected_cases", SUITES)
def test_wycheproof_p1363_suite(filename: str, expected_cases: int) -> None:
    """Every case in one suite, verdict by verdict.

    A Wycheproof `result` is `valid` or `invalid` and nothing else in these four
    files, so the assertion is exact in both directions: a valid signature this
    module rejects is as much a defect as an invalid one it accepts."""
    suite = _load(WYCHEPROOF / filename)
    assert suite["schema"] == "ecdsa_p1363_verify_schema_v1.json", (
        f"{filename} is not a P1363 verification suite. `tee_quote` signatures "
        "are raw R||S with no ASN.1 wrapper; a DER suite tests a parser this "
        "module does not have."
    )

    seen = 0
    wrong: list[str] = []
    for group in suite["testGroups"]:
        curve = _curve_for(group)
        public_key = bytes.fromhex(group["publicKey"]["uncompressed"])
        for case in group["tests"]:
            seen += 1
            message = bytes.fromhex(case["msg"])
            signature = bytes.fromhex(case["sig"])
            got = ecdsa_verify(curve, public_key, message, signature)
            want = case["result"] == "valid"
            if got is not want:
                verb = "accepted" if got else "rejected"
                wrong.append(
                    f"tcId {case['tcId']} ({case['comment']}, "
                    f"flags={case.get('flags')}): Wycheproof says "
                    f"{case['result']}, ecdsa_verify {verb} it"
                )

    assert seen == expected_cases, (
        f"{filename} holds {seen} cases, not the {expected_cases} that were "
        "committed. Update the count deliberately; do not let the suite shrink "
        "under a passing run."
    )
    assert not wrong, (
        f"{len(wrong)} of {seen} {filename} cases disagree with the "
        "implementation:\n  " + "\n  ".join(wrong[:20])
    )


def test_the_committed_suites_still_carry_the_families_they_are_here_for():
    """The pass count is not the coverage; the flags are."""
    present: set[str] = set()
    total = 0
    for filename, _ in SUITES:
        for group in _load(WYCHEPROOF / filename)["testGroups"]:
            for case in group["tests"]:
                total += 1
                present.update(case.get("flags", ()))

    assert total == TOTAL_WYCHEPROOF_CASES, (
        f"the four committed suites hold {total} cases, not "
        f"{TOTAL_WYCHEPROOF_CASES}."
    )
    missing = sorted(REQUIRED_FLAGS - present)
    assert not missing, (
        f"the committed Wycheproof fixtures no longer contain any case flagged "
        f"{missing}. Those are the attack families these vectors are carried "
        "for; a refresh that drops one silently removes the coverage."
    )


def test_wycheproof_public_keys_decode_to_the_coordinates_the_suite_names():
    """The `uncompressed` blob and the `wx`/`wy` integers must agree.

    Verification against the wrong point passes no vector and fails no vector:
    it just makes every case in the group meaningless in the same direction.
    This reads the key two ways and insists they are the same key."""
    checked = 0
    for filename, _ in SUITES:
        for group in _load(WYCHEPROOF / filename)["testGroups"]:
            curve = _curve_for(group)
            raw = bytes.fromhex(group["publicKey"]["uncompressed"])
            width = curve.coord_len
            assert raw[0] == 0x04 and len(raw) == 2 * width + 1
            x = int(group["publicKey"]["wx"], 16)
            y = int(group["publicKey"]["wy"], 16)
            assert int.from_bytes(raw[1:1 + width], "big") == x
            assert int.from_bytes(raw[1 + width:], "big") == y
            checked += 1
    assert checked >= 100, f"only {checked} groups read; the fixtures are thin"


# --------------------------------------------------------------------------
# RFC 6979: the nonce itself, not just the signature it happens to produce
# --------------------------------------------------------------------------

RFC6979 = _load(FIXTURES / "rfc6979_ecdsa_p256_p384.json")
RFC6979_BY_NAME = {"P-256": CURVE_P256, "P-384": CURVE_P384}
RFC6979_CASES = [
    (group["curve"], index)
    for group in RFC6979["groups"]
    for index in range(len(group["cases"]))
]


def _rfc_group(curve_name: str) -> dict:
    for group in RFC6979["groups"]:
        if group["curve"] == curve_name:
            return group
    raise AssertionError(f"no RFC 6979 group for {curve_name}")


@pytest.mark.parametrize("curve_name", ["P-256", "P-384"])
def test_rfc6979_key_pair(curve_name: str) -> None:
    """The RFC's own key pair, re-derived from the private scalar.

    This is the cheapest check that `_mul` and the generator agree with the
    standard: a wrong `gy`, a wrong `p` or a double-and-add that drops a bit
    lands somewhere other than the point the RFC prints."""
    group = _rfc_group(curve_name)
    curve = RFC6979_BY_NAME[curve_name]
    assert int(group["q"], 16) == curve.n, (
        f"the {curve_name} order in tee_quote is not the q RFC 6979 prints"
    )
    expected = bytes.fromhex(group["ux"] + group["uy"])
    assert derive_public_key(curve, int(group["x"], 16)) == expected


@pytest.mark.parametrize("curve_name,index", RFC6979_CASES)
def test_rfc6979_deterministic_nonce_and_signature(curve_name: str,
                                                   index: int) -> None:
    """`k`, `r` and `s` all three, against the RFC's printed values.

    Asserting only `(r, s)` would let a nonce derivation that is wrong but
    self-consistent through, because the verifier would still accept what the
    signer produced. `k` is the value RFC 6979 exists to pin, so it is the value
    asserted.

    The five hashes per curve are the coverage that matters. SHA-1 and SHA-224
    are narrower than either order, so the HMAC drum has to turn more than once
    and the result must NOT be truncated; SHA-256 is narrower than P-384's
    order and exactly P-256's; SHA-384 and SHA-512 are wider than P-256's order,
    so the digest has to be cut to the leftmost `qlen` bits. One `_bits2int`
    serves all of it."""
    group = _rfc_group(curve_name)
    case = group["cases"][index]
    curve = dataclasses.replace(RFC6979_BY_NAME[curve_name],
                                digest=case["hash"])
    private_key = int(group["x"], 16)
    message = case["message"].encode("ascii")

    nonce = _rfc6979_k(curve, private_key, curve.hash(message))
    assert nonce == int(case["k"], 16), (
        f"RFC 6979 {curve_name}/{case['hash']} over {case['message']!r}: the "
        f"deterministic nonce is {nonce:x}, the RFC says {case['k'].lower()}"
    )

    signature = ecdsa_sign(curve, private_key, message)
    width = curve.order_len
    assert len(signature) == 2 * width
    assert int.from_bytes(signature[:width], "big") == int(case["r"], 16)
    assert int.from_bytes(signature[width:], "big") == int(case["s"], 16)

    public_key = bytes.fromhex(group["ux"] + group["uy"])
    assert ecdsa_verify(curve, public_key, message, signature) is True


def test_rfc6979_fixture_is_not_vacuous():
    """Twenty cases, ten per curve, five hashes each over two messages."""
    assert len(RFC6979["groups"]) == 2
    for group in RFC6979["groups"]:
        assert len(group["cases"]) == 10, (
            f"the {group['curve']} RFC 6979 group has "
            f"{len(group['cases'])} cases, not 10"
        )
        hashes = {case["hash"] for case in group["cases"]}
        assert hashes == {"sha1", "sha224", "sha256", "sha384", "sha512"}, (
            f"the {group['curve']} group no longer spans the five hash widths "
            f"that exercise _bits2int in both directions: {sorted(hashes)}"
        )


# --------------------------------------------------------------------------
# `_bits2int` on its own, in both directions
# --------------------------------------------------------------------------


def test_bits2int_takes_the_leftmost_qlen_bits_of_a_wide_digest():
    """A 512-bit digest against a 256-bit order keeps the TOP 256 bits.

    An implementation that reduces modulo `n` instead, or that keeps the bottom
    bits, verifies its own signatures perfectly and disagrees with every other
    ECDSA in the world. The P-256/SHA-512 Wycheproof suite is the systemic
    check; this is the unit that says which way round it is."""
    digest = bytes(range(64))
    assert _bits2int(digest, 256) == int.from_bytes(digest[:32], "big")
    assert _bits2int(bytes.fromhex("ff" * 64), 256) == (1 << 256) - 1


def test_bits2int_leaves_a_narrow_digest_alone():
    """A 256-bit digest against a 384-bit order is used whole, unshifted.

    Not left-padded into the high bits, which is the mirror-image mistake."""
    digest = bytes(range(32))
    assert _bits2int(digest, 384) == int.from_bytes(digest, "big")
    assert _bits2int(digest, 256) == int.from_bytes(digest, "big")

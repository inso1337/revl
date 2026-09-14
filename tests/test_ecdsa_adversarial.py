"""The specific ways a hand-rolled ECDSA verifier is wrong, one at a time.

`tests/test_ecdsa_vectors.py` runs 1192 Wycheproof cases and 20 RFC 6979 cases
through `src/revl/tee_quote.py`. That is the systemic check, and it is the one
that would catch an unknown defect. This file is the NAMED one: each test here
is a single documented failure mode, asserted on its own, so that when one of
them breaks the failure says which property was lost rather than "17 of 262
vectors disagree".

The two are not redundant. A vector suite tells you the implementation is wrong;
a named property tells you which guarantee the attestation root just stopped
providing. A quote verifier decides whether a peer's claim about the hardware it
runs on is genuine, so "which guarantee" is the part somebody has to act on.

One behaviour here is characterised rather than forbidden: ECDSA signatures are
malleable, `(r, n - s)` verifies wherever `(r, s)` does, and this module does
not enforce low-s. See `test_a_signature_is_malleable_and_nothing_here_pretends_
otherwise` for why that is sound for THIS protocol and what would make it stop
being sound.

Out of scope, stated rather than implied: timing. `_mul` is variable time by
construction, as its own docstring says. Nothing here measures it.
"""

from __future__ import annotations

import dataclasses

import pytest

from revl.tee_quote import (
    CURVE_P256,
    CURVE_P384,
    Curve,
    QuoteFormatError,
    derive_public_key,
    ecdsa_sign,
    ecdsa_verify,
    _add,
    _mul,
    _on_curve,
    _decode_public_key,
)

CURVES = [CURVE_P256, CURVE_P384]
CURVE_IDS = [curve.name for curve in CURVES]

#: One fixed key pair per curve, and the message they sign. Deterministic on
#: purpose: a failure here has to be reproducible from the file.
SCALARS = {
    "P-256": 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721,
    "P-384": int(
        "6B9D3DAD2E1B8C1C05B19875B6659F4DE23C3B667BF297BA9AA47740787137D8"
        "96D5724E4C70A825F872C9EA60D2EDF5", 16),
}
MESSAGE = b"an attestation quote is a claim about hardware"


def _keypair(curve: Curve):
    scalar = SCALARS[curve.name]
    return scalar, derive_public_key(curve, scalar)


def _signature(curve: Curve):
    scalar, public_key = _keypair(curve)
    signature = ecdsa_sign(curve, scalar, MESSAGE)
    width = curve.order_len
    r = int.from_bytes(signature[:width], "big")
    s = int.from_bytes(signature[width:], "big")
    return public_key, signature, r, s


def _pack(curve: Curve, r: int, s: int) -> bytes:
    """`R || S` for values that may not fit the order length, so the range
    checks can be probed with genuinely out-of-range integers."""
    width = curve.order_len
    modulus = 1 << (8 * width)
    return (r % modulus).to_bytes(width, "big") + (s % modulus).to_bytes(width, "big")


# --------------------------------------------------------------------------
# The baseline. If this fails nothing else in the file means anything.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("curve", CURVES, ids=CURVE_IDS)
def test_the_fixture_signature_verifies(curve: Curve) -> None:
    public_key, signature, r, s = _signature(curve)
    assert ecdsa_verify(curve, public_key, MESSAGE, signature) is True
    assert 1 <= r < curve.n and 1 <= s < curve.n


# --------------------------------------------------------------------------
# Invalid-curve: is `_on_curve` CALLED, or only defined?
# --------------------------------------------------------------------------


@pytest.mark.parametrize("curve", CURVES, ids=CURVE_IDS)
def test_a_public_key_off_the_curve_is_refused(curve: Curve) -> None:
    """The invalid-curve attack, in its simplest form.

    An implementation that decodes X and Y and multiplies without asking
    whether the point satisfies the curve equation is doing arithmetic in a
    group the attacker chose. `_on_curve` exists in this module; the question a
    test has to answer is whether anything CALLS it, and the only way to answer
    it is through the public entry point."""
    _, public_key = _keypair(curve)
    width = curve.coord_len
    x = int.from_bytes(public_key[:width], "big")
    y = int.from_bytes(public_key[width:], "big")

    off_curve = (y + 1) % curve.p
    assert not _on_curve(curve, (x, off_curve))
    tampered = x.to_bytes(width, "big") + off_curve.to_bytes(width, "big")

    _, signature, _, _ = _signature(curve)
    with pytest.raises(QuoteFormatError, match="not a point on the curve"):
        ecdsa_verify(curve, tampered, MESSAGE, signature)
    with pytest.raises(QuoteFormatError, match="not a point on the curve"):
        _decode_public_key(curve, tampered)


@pytest.mark.parametrize("curve", CURVES, ids=CURVE_IDS)
def test_a_public_key_with_a_coordinate_at_or_past_the_prime_is_refused(
        curve: Curve) -> None:
    """`x = p` is `x = 0` to anything that reduces before it checks."""
    _, public_key = _keypair(curve)
    width = curve.coord_len
    y = public_key[width:]
    for coordinate in (curve.p, curve.p + 1, (1 << (8 * width)) - 1):
        if coordinate.bit_length() > 8 * width:
            continue
        raw = coordinate.to_bytes(width, "big") + y
        with pytest.raises(QuoteFormatError):
            ecdsa_verify(curve, raw, MESSAGE, b"\x00" * (2 * curve.order_len))
    assert not _on_curve(curve, (curve.p, int.from_bytes(y, "big")))
    assert not _on_curve(curve, (0, curve.p))
    assert not _on_curve(curve, (-1, 1))


@pytest.mark.parametrize("curve", CURVES, ids=CURVE_IDS)
def test_the_all_zero_public_key_is_refused_as_the_point_at_infinity(
        curve: Curve) -> None:
    """`(0, 0)` is the identity as these formats spell it, and the identity
    verifies everything or nothing depending on the bug."""
    _, signature, _, _ = _signature(curve)
    with pytest.raises(QuoteFormatError, match="point at infinity"):
        ecdsa_verify(curve, bytes(2 * curve.coord_len), MESSAGE, signature)


# --------------------------------------------------------------------------
# Range checks on r and s
# --------------------------------------------------------------------------


@pytest.mark.parametrize("curve", CURVES, ids=CURVE_IDS)
def test_r_or_s_outside_one_to_n_minus_one_is_refused(curve: Curve) -> None:
    """`1 <= r <= n-1` and `1 <= s <= n-1`, both ends, both scalars.

    `r = s = 0` is the admission behind CVE-2022-21449: a verifier that skips
    the range check and computes with a zero scalar can be handed a signature
    forged without the key and without the message. `r = n` and `s = n` are the
    same hole one step along, reachable by an implementation that reduces
    modulo `n` before it compares instead of after."""
    public_key, _, r, s = _signature(curve)
    n = curve.n
    cases = {
        "r=0": (0, s),
        "s=0": (r, 0),
        "r=0,s=0": (0, 0),
        "r=n": (n, s),
        "s=n": (r, n),
        "r=n+1": (n + 1, s),
        "s=n+1": (r, n + 1),
        "r=r+n": (r + n, s),
        "s=s+n": (r, s + n),
        "r=all-ones": ((1 << (8 * curve.order_len)) - 1, s),
        "s=all-ones": (r, (1 << (8 * curve.order_len)) - 1),
    }
    accepted = [
        name for name, (rr, ss) in cases.items()
        if ecdsa_verify(curve, public_key, MESSAGE, _pack(curve, rr, ss))
    ]
    assert not accepted, (
        f"{curve.name}: ecdsa_verify accepted out-of-range scalars {accepted}. "
        "A signature whose r or s lies outside [1, n-1] is not a signature."
    )


@pytest.mark.parametrize("curve", CURVES, ids=CURVE_IDS)
def test_a_signature_of_the_wrong_length_is_refused_rather_than_padded(
        curve: Curve) -> None:
    """Raw R||S carries no length, so the length IS the parse.

    A verifier that left-pads a short signature reads a different integer than
    the signer wrote, and one that ignores trailing bytes lets a signature carry
    a payload. Both answer False here, not an exception: a malformed signature
    is the PEER's bytes, and the module's contract is that only a malformed
    verifier key raises."""
    public_key, signature, _, _ = _signature(curve)
    for bad in (signature[:-1], signature[1:], signature + b"\x00",
                b"\x00" + signature, b"", signature[: len(signature) // 2]):
        assert ecdsa_verify(curve, public_key, MESSAGE, bad) is False
    for not_bytes in (None, 0, signature.hex(), list(signature)):
        assert ecdsa_verify(curve, public_key, MESSAGE, not_bytes) is False


@pytest.mark.parametrize("curve", CURVES, ids=CURVE_IDS)
def test_a_signature_over_a_different_message_is_refused(curve: Curve) -> None:
    public_key, signature, _, _ = _signature(curve)
    assert ecdsa_verify(curve, public_key, MESSAGE + b"!", signature) is False
    assert ecdsa_verify(curve, public_key, b"", signature) is False
    flipped = bytearray(MESSAGE)
    flipped[0] ^= 0x01
    assert ecdsa_verify(curve, public_key, bytes(flipped), signature) is False


@pytest.mark.parametrize("curve", CURVES, ids=CURVE_IDS)
def test_a_signature_under_a_different_key_is_refused(curve: Curve) -> None:
    public_key, signature, _, _ = _signature(curve)
    other = derive_public_key(curve, SCALARS[curve.name] ^ 0xFFFF)
    assert other != public_key
    assert ecdsa_verify(curve, other, MESSAGE, signature) is False


# --------------------------------------------------------------------------
# Malleability: characterised, not forbidden
# --------------------------------------------------------------------------


@pytest.mark.parametrize("curve", CURVES, ids=CURVE_IDS)
def test_a_signature_is_malleable_and_nothing_here_pretends_otherwise(
        curve: Curve) -> None:
    """`(r, n - s)` verifies wherever `(r, s)` does. That is ECDSA, not a bug.

    It is written down as a test rather than left implicit because whether it
    MATTERS is a property of the protocol above, not of the arithmetic. It is
    sound here for one reason: replay in `tee_quote` is refused by binding the
    quote's `report_data` to the verifier's challenge digest, so a second
    encoding of the same signature is a second copy of the same statement about
    the same challenge and buys an attacker nothing.

    What would make it stop being sound: keying anything on the signature bytes.
    A seen-quotes cache, a dedup set, a receipt id or an idempotency key derived
    from the signature all become forgeable the moment a third party can mint an
    unlimited number of distinct byte strings that verify. If such a thing is
    ever added, this test is the place that says the assumption changed, and the
    fix is to enforce low-s (`s <= n // 2`) in `ecdsa_verify`.

    `(n - r, s)` is NOT a valid second encoding, and is asserted here too so the
    test cannot be read as "any mutation verifies"."""
    public_key, signature, r, s = _signature(curve)
    mauled = _pack(curve, r, curve.n - s)
    assert mauled != signature
    assert ecdsa_verify(curve, public_key, MESSAGE, mauled) is True, (
        "textbook ECDSA accepts (r, n-s); if this now fails, low-s enforcement "
        "was added and the docstring above needs updating rather than the test"
    )
    assert ecdsa_verify(curve, public_key, MESSAGE,
                        _pack(curve, curve.n - r, s)) is False
    assert ecdsa_verify(curve, public_key, MESSAGE,
                        _pack(curve, s, r)) is False


# --------------------------------------------------------------------------
# The group law: identity, doubling, and the scalar edges
# --------------------------------------------------------------------------


@pytest.mark.parametrize("curve", CURVES, ids=CURVE_IDS)
def test_point_at_infinity_is_the_identity_of_add(curve: Curve) -> None:
    generator = (curve.gx, curve.gy)
    negated = (curve.gx, (-curve.gy) % curve.p)
    assert _add(curve, None, None) is None
    assert _add(curve, generator, None) == generator
    assert _add(curve, None, generator) == generator
    assert _add(curve, generator, negated) is None, (
        "P + (-P) must be the point at infinity, not a division by zero and "
        "not a garbage affine point"
    )
    assert _add(curve, negated, generator) is None


@pytest.mark.parametrize("curve", CURVES, ids=CURVE_IDS)
def test_add_doubles_rather_than_dividing_by_zero_on_equal_points(
        curve: Curve) -> None:
    """`P + P` has to take the tangent, not the chord.

    The two branches are selected on `x1 == x2`, and the wrong selection is
    either a `ZeroDivisionError` or, worse, a silently wrong point."""
    generator = (curve.gx, curve.gy)
    doubled = _add(curve, generator, generator)
    assert doubled is not None and _on_curve(curve, doubled)
    assert doubled == _mul(curve, 2, generator)
    assert _add(curve, doubled, generator) == _mul(curve, 3, generator)


@pytest.mark.parametrize("curve", CURVES, ids=CURVE_IDS)
def test_scalar_multiplication_at_the_edges_of_the_order(curve: Curve) -> None:
    """0, 1, 2, n-1, n, n+1, and the identity as an operand.

    `n * G` is the point at infinity and `(n+1) * G` is `G` again. An
    implementation that loses the top bit, or that treats `None` as an error
    rather than as the identity, gets one of these wrong."""
    generator = (curve.gx, curve.gy)
    negated = (curve.gx, (-curve.gy) % curve.p)

    assert _mul(curve, 0, generator) is None
    assert _mul(curve, 1, generator) == generator
    assert _mul(curve, 2, generator) == _add(curve, generator, generator)
    assert _mul(curve, curve.n - 1, generator) == negated
    assert _mul(curve, curve.n, generator) is None, (
        "n * G is the point at infinity; the generator's order IS n"
    )
    assert _mul(curve, curve.n + 1, generator) == generator
    assert _mul(curve, 7, None) is None
    assert _mul(curve, 0, None) is None

    for scalar in (3, 5, 1 << 64, curve.n - 2):
        point = _mul(curve, scalar, generator)
        assert point is not None and _on_curve(curve, point)


@pytest.mark.parametrize("curve", CURVES, ids=CURVE_IDS)
def test_the_curve_parameters_are_self_consistent(curve: Curve) -> None:
    """The generator is on the curve, the order is what the parameters say, and
    `a = -3`. `p` is written as a formula in the module; this is the other half
    of that check."""
    assert curve.a == curve.p - 3
    assert _on_curve(curve, (curve.gx, curve.gy))
    assert curve.n != curve.p, "the order and the field prime are not the same"
    assert _mul(curve, curve.n, (curve.gx, curve.gy)) is None


# --------------------------------------------------------------------------
# Public key encoding
# --------------------------------------------------------------------------


@pytest.mark.parametrize("curve", CURVES, ids=CURVE_IDS)
def test_public_key_encodings_that_are_accepted_and_refused(
        curve: Curve) -> None:
    """Raw `X || Y`, and the SEC1 uncompressed prefix, and nothing else.

    Compressed points are REFUSED rather than decompressed. That is a narrowing
    and it is the right one for this module: neither quote format carries a
    compressed key, and a decompression routine is more curve arithmetic in the
    security path with no caller."""
    _, public_key = _keypair(curve)
    _, signature, _, _ = _signature(curve)
    width = curve.coord_len
    x = int.from_bytes(public_key[:width], "big")
    y = int.from_bytes(public_key[width:], "big")

    assert ecdsa_verify(curve, public_key, MESSAGE, signature) is True
    assert ecdsa_verify(curve, b"\x04" + public_key, MESSAGE,
                        signature) is True

    compressed = bytes([0x02 + (y & 1)]) + x.to_bytes(width, "big")
    refused = {
        "compressed": compressed,
        "wrong leading byte": b"\x05" + public_key,
        "infinity byte": b"\x00",
        "one short": public_key[:-1],
        "one long": public_key + b"\x00",
        "empty": b"",
        "x only": public_key[:width],
        "double prefix": b"\x04\x04" + public_key,
    }
    for name, raw in refused.items():
        try:
            verdict = ecdsa_verify(curve, raw, MESSAGE, signature)
        except QuoteFormatError:
            continue
        raise AssertionError(
            f"{curve.name}: a {name} public key was not refused; "
            f"ecdsa_verify returned {verdict!r}")

    for name, raw in {"str": public_key.hex(), "int": x, "none": None,
                      "list": list(public_key)}.items():
        with pytest.raises(QuoteFormatError, match="raw X||Y bytes"):
            ecdsa_verify(curve, raw, MESSAGE, signature)

    assert _decode_public_key(curve, bytearray(public_key)) == (x, y)


@pytest.mark.parametrize("curve", CURVES, ids=CURVE_IDS)
def test_a_private_scalar_outside_one_to_n_minus_one_is_refused(
        curve: Curve) -> None:
    for scalar in (0, -1, curve.n, curve.n + 1, 1 << 400):
        with pytest.raises(QuoteFormatError):
            derive_public_key(curve, scalar)
    with pytest.raises(QuoteFormatError):
        derive_public_key(curve, True)
    assert derive_public_key(curve, 1) == (
        curve.gx.to_bytes(curve.coord_len, "big")
        + curve.gy.to_bytes(curve.coord_len, "big"))


# --------------------------------------------------------------------------
# The curve is bound to its hash by the caller, so check the wiring
# --------------------------------------------------------------------------


def test_each_curve_carries_the_hash_its_quote_format_uses():
    """P-256 with SHA-256 (Intel TDX), P-384 with SHA-384 (AMD SEV-SNP).

    Both are the FIPS 186-4 pairing, where the digest is exactly the order
    width. A quote signed over one digest and verified against another is a
    signature over a different message and would fail closed, but it would fail
    for the wrong reason and be read as a corrupt quote."""
    assert CURVE_P256.digest == "sha256"
    assert CURVE_P384.digest == "sha384"
    assert CURVE_P256.n.bit_length() == 256
    assert CURVE_P384.n.bit_length() == 384
    assert CURVE_P256.order_len == 32 and CURVE_P256.coord_len == 32
    assert CURVE_P384.order_len == 48 and CURVE_P384.coord_len == 48


@pytest.mark.parametrize("curve", CURVES, ids=CURVE_IDS)
def test_a_signature_does_not_carry_across_curves(curve: Curve) -> None:
    """The same key and message on P-256 and P-384 produce unrelated bytes, and
    neither verifies as the other. Trivial, and it is the check that a curve
    parameter is actually read from the `Curve` rather than captured."""
    other = CURVE_P384 if curve is CURVE_P256 else CURVE_P256
    public_key, signature, _, _ = _signature(curve)
    assert len(signature) != len(ecdsa_sign(other, SCALARS[other.name],
                                            MESSAGE))
    assert ecdsa_verify(other, derive_public_key(other, SCALARS[other.name]),
                        MESSAGE, signature) is False


@pytest.mark.parametrize("curve", CURVES, ids=CURVE_IDS)
def test_the_digest_is_read_from_the_curve_not_hardcoded(curve: Curve) -> None:
    """Rebinding the hash changes the signature, and a signature made under one
    hash does not verify under another. `_bits2int`'s truncation path is only
    reachable this way, so the rebinding has to actually take effect."""
    scalar, public_key = _keypair(curve)
    wide = dataclasses.replace(curve, digest="sha512")
    baseline = ecdsa_sign(curve, scalar, MESSAGE)
    rebound = ecdsa_sign(wide, scalar, MESSAGE)
    assert baseline != rebound
    assert ecdsa_verify(wide, public_key, MESSAGE, rebound) is True
    assert ecdsa_verify(curve, public_key, MESSAGE, rebound) is False
    assert ecdsa_verify(wide, public_key, MESSAGE, baseline) is False

"""Real TEE quote formats, and an attestation root the peer does not hold
(roadmap item 475, issue #827).

``tee_attestation`` owns what a placement DEMANDS and the decision that demand
encodes. It shipped with one signature algorithm, a symmetric HMAC, and said so:
a MAC proves that the holder of a key authored a record, so a peer holding the
attester key can mint a proof of a bundle it never ran. The key separation there
(`attester_key != peer_key`) is the best a symmetric primitive allows, and it is
not an attestation root: it is an assertion by a second party the verifier also
has to trust with a secret.

This module is the root. It parses the two quote formats a confidential worker
actually produces, verifies them with real asymmetric crypto, and terminates the
trust chain in a key the verifier PINNED out of band — so a peer that forges or
replays a quote is refused rather than admitted, and the peer's own keys are
never anywhere in the chain.

What is implemented
-------------------
* **Intel TDX, Quote v4 / ECDSA-256-with-P-256** (:func:`parse_tdx_quote`). The
  real wire layout: the 48-byte quote header, the 584-byte TD Quote Body (whose
  `mrtd` is the initial TD measurement and whose 64-byte `reportdata` is the
  only field the workload controls), then the ECDSA-P256 authentication data —
  the quote signature over `header || td_quote_body` by the attestation key, the
  attestation key itself, the 384-byte QE Report that certifies it, the QE
  Report's signature by the platform (PCK) key, the QE authentication data and
  the certification data.
* **AMD SEV-SNP, ATTESTATION_REPORT v2/v3** (:func:`parse_sev_snp_report`). The
  real 0x4A0-byte layout, its 48-byte `measurement`, its 64-byte `report_data`,
  and the 512-byte trailing signature: ECDSA-P384 R and S as 72-byte
  LITTLE-endian integers, over the report's first 0x2A0 bytes, by the VCEK.
* **ECDSA over NIST P-256 and P-384** (:func:`ecdsa_verify`), in terms of
  `hashlib` and integer arithmetic only. `revl` ships with no runtime
  dependencies (see pyproject) and the standard library has no asymmetric
  primitive, so the alternative to ~150 lines of curve arithmetic here is a
  dependency in the security path of a compiler. The prime of each curve is
  written as the FORMULA the standard defines rather than a transcribed
  constant, and `tests/test_tee_quote.py` re-derives the generator's membership
  and order from the parameters, so a typo in a constant reddens rather than
  quietly verifying nothing.

The root, and why it is not the record's business
-------------------------------------------------
:class:`HardwareRoot` holds what the OPERATOR pinned: platform keys by
fingerprint (``platform_keys``) and/or vendor root keys that may certify a
platform key (``endorsement_roots``). A quote is verified against the platform
key the ROOT names for the fingerprint the record carries. The root is never
read out of the record, and a record naming a fingerprint the root does not hold
is refused — so "bring your own root" is not a move a peer can make.

A one-hop :class:`PlatformEndorsement` stands in for the vendor's own X.509
chain (Intel's PCK chain to the SGX Root CA, AMD's VCEK certificate under the
ASK and ARK). It is the same shape: a record, signed by a pinned root key, that
binds a platform key to a vendor and a validity window. Consuming the vendor
DER chain directly is the remaining hop and is named as such in
`docs/tee-attestation-root.md`; it changes which bytes carry the endorsement, not
where the trust terminates.

:class:`DevMacRoot` wraps the pre-existing symmetric verifier. It exists because
removing it would break every caller that has only a shared secret, and it is
NOT a production root: ``is_production`` is ``False``, constructing one requires
passing ``acknowledged_dev_only=True`` by name, and every verdict reached through
it is labelled in its own reason text. A caller that wants the guarantee asks
for it — :func:`require_production_root`.

Fail-closed
-----------
Every function here refuses rather than guesses. A truncated quote, an unknown
version, a tee type that is not TDX, an attestation-key type that is not the one
the format declares, a signature-data length that disagrees with the bytes
present, a non-canonical scalar (zero, or at or above the group order), a public
key that is not on the curve, a point at infinity, a report_data that does not
match the challenge digest, a fingerprint the root does not hold, an endorsement
outside its window: each is a named refusal, never an admission.
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from .attest import NotCanonicalizable, _canonical_bytes, key_id

__all__ = [
    "QuoteFormatError",
    "RootError",
    "Curve",
    "CURVE_P256",
    "CURVE_P384",
    "CURVES",
    "ecdsa_verify",
    "ecdsa_sign",
    "derive_public_key",
    "private_key_from_seed",
    "public_key_id",
    "TdxQuote",
    "SevSnpReport",
    "QuoteVerdict",
    "parse_tdx_quote",
    "parse_sev_snp_report",
    "build_tdx_quote",
    "build_sev_snp_report",
    "AttestationRoot",
    "HardwareRoot",
    "DevMacRoot",
    "PlatformEndorsement",
    "sign_endorsement",
    "verify_endorsement",
    "verify_quote",
    "require_production_root",
    "SIGN_ALG_TDX",
    "SIGN_ALG_SEV_SNP",
    "QUOTE_SIGN_ALGS",
    "TDX_QUOTE_VERSION",
    "TDX_TEE_TYPE",
    "INTEL_QE_VENDOR_ID",
    "SEV_SNP_REPORT_SIZE",
    "PinnedKey",
    "DEV_ROOT_NOTE",
    "VENDOR_INTEL_TDX",
    "VENDOR_AMD_SEV_SNP",
]


class QuoteFormatError(ValueError):
    """A quote's BYTES are not the format they claim to be: too short, a version
    or tee type this parser does not implement, an internal length that
    disagrees with the bytes present. Raised by the parsers; every verifier
    catches it and returns a refusal, so a hostile blob cannot break a caller
    that iterates candidate peers."""


class RootError(ValueError):
    """An attestation root is misconfigured at construction time: no pinned key
    at all, a pinned key that is not a point on its curve, a development
    verifier selected without saying so. A misconfigured root is refused where
    it is built rather than silently admitting nothing (or everything)."""


# --------------------------------------------------------------------------
# ECDSA over NIST P-256 / P-384, in `hashlib` and integers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Curve:
    """A short-Weierstrass prime curve `y^2 = x^3 - 3x + b` over `GF(p)`.

    Both NIST curves this module needs have `a = -3`, which is why `a` is not a
    member. `p` is supplied by its defining formula rather than as a transcribed
    hex constant: the two are checkable against each other and only one of them
    can be mistyped without a test noticing."""

    name: str
    p: int
    b: int
    gx: int
    gy: int
    n: int
    digest: str

    @property
    def a(self) -> int:
        return self.p - 3

    @property
    def coord_len(self) -> int:
        """Bytes per affine coordinate. A public key is `X || Y`, each padded to
        this length, which is the raw form both vendor formats carry."""
        return (self.p.bit_length() + 7) // 8

    @property
    def order_len(self) -> int:
        return (self.n.bit_length() + 7) // 8

    def hash(self, message: bytes) -> bytes:
        return hashlib.new(self.digest, message).digest()


#: secp256r1 / prime256v1. `p = 2^256 - 2^224 + 2^192 + 2^96 - 1` (FIPS 186-4
#: D.1.2.3). The TDX quote's attestation key and PCK key live on this curve.
CURVE_P256 = Curve(
    name="P-256",
    p=2**256 - 2**224 + 2**192 + 2**96 - 1,
    b=0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B,
    gx=0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    gy=0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
    n=0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551,
    digest="sha256",
)

#: secp384r1. `p = 2^384 - 2^128 - 2^96 + 2^32 - 1` (FIPS 186-4 D.1.2.4). The
#: SEV-SNP VCEK lives on this curve, with SHA-384.
CURVE_P384 = Curve(
    name="P-384",
    p=2**384 - 2**128 - 2**96 + 2**32 - 1,
    b=0xB3312FA7E23EE7E4988E056BE3F82D19181D9C6EFE8141120314088F5013875AC656398D8A2ED19D2A85C8EDD3EC2AEF,
    gx=0xAA87CA22BE8B05378EB1C71EF320AD746E1D3B628BA79B9859F741E082542A385502F25DBF55296C3A545E3872760AB7,
    gy=0x3617DE4A96262C6F5D9E98BF9292DC29F8F41DBD289A147CE9DA3113B5F0B8C00A60B1CE1D7E819D7A431D7C90EA0E5F,
    n=0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFC7634D81F4372DDF581A0DB248B0A77AECEC196ACCC52973,
    digest="sha384",
)

CURVES: dict[str, Curve] = {CURVE_P256.name: CURVE_P256, CURVE_P384.name: CURVE_P384}

_Point = Optional[tuple[int, int]]


def _add(curve: Curve, left: _Point, right: _Point) -> _Point:
    """Affine point addition. ``None`` is the point at infinity."""
    if left is None:
        return right
    if right is None:
        return left
    p = curve.p
    if (left[0] - right[0]) % p == 0:
        if (left[1] + right[1]) % p == 0:
            return None
        slope = (3 * left[0] * left[0] + curve.a) * pow(2 * left[1], -1, p) % p
    else:
        slope = (right[1] - left[1]) * pow(right[0] - left[0], -1, p) % p
    x = (slope * slope - left[0] - right[0]) % p
    return x, (slope * (left[0] - x) - left[1]) % p


def _mul(curve: Curve, scalar: int, point: _Point) -> _Point:
    """Double-and-add. Not constant time, and deliberately so: every scalar this
    module multiplies by is PUBLIC (a verification scalar, or a fixture key that
    signs nothing secret), so there is no secret to leak through the timing and
    no reason to hand-roll a hardened ladder in a compiler."""
    result: _Point = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _add(curve, result, addend)
        addend = _add(curve, addend, addend)
        scalar >>= 1
    return result


def _on_curve(curve: Curve, point: tuple[int, int]) -> bool:
    x, y = point
    if not (0 <= x < curve.p and 0 <= y < curve.p):
        return False
    return (y * y - x**3 - curve.a * x - curve.b) % curve.p == 0


def _decode_public_key(curve: Curve, raw: Any) -> tuple[int, int]:
    """A raw `X || Y` public key as an affine point, refusing anything that is
    not one. An off-curve point, the point at infinity and a wrong length are
    each a refusal: verification against a point that is not on the curve is not
    verification."""
    if not isinstance(raw, (bytes, bytearray)):
        raise QuoteFormatError(
            f"a public key must be raw X||Y bytes, got {type(raw).__name__}")
    width = curve.coord_len
    body = bytes(raw)
    if len(body) == 2 * width + 1 and body[0] == 0x04:
        body = body[1:]  # the SEC1 uncompressed prefix, tolerated on input
    if len(body) != 2 * width:
        raise QuoteFormatError(
            f"a {curve.name} public key is {2 * width} bytes of raw X||Y, got "
            f"{len(body)}")
    x = int.from_bytes(body[:width], "big")
    y = int.from_bytes(body[width:], "big")
    if x == 0 and y == 0:
        raise QuoteFormatError(
            f"the {curve.name} public key is the point at infinity, which "
            f"verifies nothing")
    if not _on_curve(curve, (x, y)):
        raise QuoteFormatError(
            f"the {curve.name} public key is not a point on the curve")
    return x, y


def _encode_public_key(curve: Curve, point: tuple[int, int]) -> bytes:
    width = curve.coord_len
    return point[0].to_bytes(width, "big") + point[1].to_bytes(width, "big")


def derive_public_key(curve: Curve, private_key: int) -> bytes:
    """The raw `X || Y` public key for a private scalar."""
    if not isinstance(private_key, int) or isinstance(private_key, bool):
        raise QuoteFormatError("a private key is an integer scalar")
    if not 1 <= private_key < curve.n:
        raise QuoteFormatError(
            f"a {curve.name} private scalar must lie in [1, n); got one that "
            f"does not")
    point = _mul(curve, private_key, (curve.gx, curve.gy))
    if point is None:  # unreachable for a scalar in [1, n)
        raise QuoteFormatError("the derived public key is the point at infinity")
    return _encode_public_key(curve, point)


def private_key_from_seed(curve: Curve, seed: bytes) -> int:
    """A private scalar derived deterministically from a seed.

    Used to build reproducible FIXTURES: a committed known-good quote has to be
    regenerable from the bytes in the repository, and a random key would make
    the fixture unreviewable. It is not a key-generation routine for production
    material and the docstring is the whole warning: the scalar is a hash of the
    seed, so its secrecy is exactly the seed's."""
    if not isinstance(seed, (bytes, bytearray)) or not seed:
        raise QuoteFormatError("a key seed must be non-empty bytes")
    counter = 0
    while True:
        stream = b""
        block = 0
        material = b"revl.tee-quote/seed\x00" + bytes(seed) + bytes([counter])
        while len(stream) < curve.order_len:
            stream += hashlib.sha512(material + bytes([block])).digest()
            block += 1
        candidate = int.from_bytes(stream[: curve.order_len], "big") % curve.n
        if candidate != 0:
            return candidate
        counter += 1


def public_key_id(public_key: bytes) -> str:
    """The non-secret fingerprint of a PUBLIC key, in the same spelling
    ``attest.key_id`` gives a symmetric one, so one `key_id` member can name
    either and a root can be indexed by it."""
    return key_id(bytes(public_key))


def _bits2int(data: bytes, qlen: int) -> int:
    value = int.from_bytes(data, "big")
    shift = len(data) * 8 - qlen
    return value >> shift if shift > 0 else value


def _rfc6979_k(curve: Curve, private_key: int, digest: bytes) -> int:
    """The deterministic nonce of RFC 6979 §3.2, with the curve's own hash.

    Deterministic signing is what makes a committed fixture reproducible: the
    same key and the same message always produce the same quote bytes, so a
    reviewer can regenerate the blob in the repository and get it back."""
    qlen = curve.n.bit_length()
    rlen = ((qlen + 7) // 8) * 8
    hlen = hashlib.new(curve.digest).digest_size
    h1 = _bits2int(digest, qlen) % curve.n
    x_octets = private_key.to_bytes(rlen // 8, "big")
    h1_octets = h1.to_bytes(rlen // 8, "big")
    v = b"\x01" * hlen
    k = b"\x00" * hlen

    def mac(key: bytes, message: bytes) -> bytes:
        return hmac.new(key, message, curve.digest).digest()

    k = mac(k, v + b"\x00" + x_octets + h1_octets)
    v = mac(k, v)
    k = mac(k, v + b"\x01" + x_octets + h1_octets)
    v = mac(k, v)
    while True:
        t = b""
        while len(t) * 8 < qlen:
            v = mac(k, v)
            t += v
        candidate = _bits2int(t, qlen)
        if 1 <= candidate < curve.n:
            return candidate
        k = mac(k, v + b"\x00")
        v = mac(k, v)


def ecdsa_sign(curve: Curve, private_key: int, message: bytes) -> bytes:
    """Deterministic ECDSA (RFC 6979) over ``message``, as raw `R || S`.

    The signer exists for the reference attester and for the fixtures: a test
    that mutates a quote and RE-SIGNS it proves the accept path is unreachable
    with a well-formed forgery, which a test that only corrupts a signature
    cannot show. The verifier is the part a deployment runs."""
    digest = curve.hash(message)
    e = _bits2int(digest, curve.n.bit_length()) % curve.n
    k = _rfc6979_k(curve, private_key, digest)
    while True:
        point = _mul(curve, k, (curve.gx, curve.gy))
        if point is not None:
            r = point[0] % curve.n
            if r != 0:
                s = (pow(k, -1, curve.n) * (e + r * private_key)) % curve.n
                if s != 0:
                    width = curve.order_len
                    return r.to_bytes(width, "big") + s.to_bytes(width, "big")
        k = (k + 1) % curve.n or 1


def ecdsa_verify(curve: Curve, public_key: bytes, message: bytes,
                 signature: bytes) -> bool:
    """Is ``signature`` (raw `R || S`, big-endian) a valid ECDSA signature over
    ``message`` under ``public_key`` (raw `X || Y`)?

    Returns ``False`` for every malformed input rather than raising, except for a
    public key that is not a point on the curve: that is the VERIFIER's own
    configuration, not the peer's record, so it raises
    :class:`QuoteFormatError` where it can be fixed. A scalar of zero or one at
    or above the group order is refused, which closes the classic "signature of
    all zeroes" admission."""
    width = curve.order_len
    if not isinstance(signature, (bytes, bytearray)) or len(signature) != 2 * width:
        return False
    point = _decode_public_key(curve, public_key)
    r = int.from_bytes(bytes(signature)[:width], "big")
    s = int.from_bytes(bytes(signature)[width:], "big")
    if not (1 <= r < curve.n and 1 <= s < curve.n):
        return False
    e = _bits2int(curve.hash(message), curve.n.bit_length()) % curve.n
    s_inv = pow(s, -1, curve.n)
    combined = _add(curve,
                    _mul(curve, e * s_inv % curve.n, (curve.gx, curve.gy)),
                    _mul(curve, r * s_inv % curve.n, point))
    if combined is None:
        return False
    return combined[0] % curve.n == r


# --------------------------------------------------------------------------
# Intel TDX: Quote v4, ECDSA-256-with-P-256
# --------------------------------------------------------------------------

#: The evidence `sign_alg` spellings these two formats answer to. They are
#: VALIDATED rather than recorded, exactly as `tee_attestation.SIGN_ALG` is, so a
#: record cannot claim one format and carry the other's bytes.
SIGN_ALG_TDX = "tdx-quote-v4-ecdsa-p256"
SIGN_ALG_SEV_SNP = "sev-snp-report-v2-ecdsa-p384"
QUOTE_SIGN_ALGS = (SIGN_ALG_TDX, SIGN_ALG_SEV_SNP)

TDX_QUOTE_VERSION = 4
#: `attestation_key_type` 2 is ECDSA-256-with-P-256, the only one Quote v4
#: defines for TDX and the only one parsed here.
TDX_ATT_KEY_TYPE_ECDSA_P256 = 2
#: `tee_type`: 0x81 is TDX. 0x00 is SGX, whose body is a different structure, so
#: it is refused by name rather than parsed as if it were a TD.
TDX_TEE_TYPE = 0x00000081
SGX_TEE_TYPE = 0x00000000
#: Intel's Quoting Enclave vendor id, the 16 bytes every Intel-issued quote
#: carries. Checked, because a quote from another vendor's QE is not a quote this
#: root's platform keys endorse.
INTEL_QE_VENDOR_ID = bytes.fromhex("939a7233f79c4ca9940a0db3957f0607")

_TDX_HEADER_LEN = 48
_TDX_BODY_LEN = 584
_SGX_REPORT_LEN = 384
# quote signature (64) + attestation key (64) + QE report (384) + its signature (64)
_TDX_SIG_FIXED_LEN = 64 + 64 + _SGX_REPORT_LEN + 64
_TDX_MIN_LEN = _TDX_HEADER_LEN + _TDX_BODY_LEN + 4

# TD Quote Body field offsets, relative to the body's own first byte.
_TD_TEE_TCB_SVN = (0, 16)
_TD_MRSEAM = (16, 64)
_TD_MRSIGNERSEAM = (64, 112)
_TD_SEAM_ATTRIBUTES = (112, 120)
_TD_ATTRIBUTES = (120, 128)
_TD_XFAM = (128, 136)
_TD_MRTD = (136, 184)
_TD_MRCONFIGID = (184, 232)
_TD_MROWNER = (232, 280)
_TD_MROWNERCONFIG = (280, 328)
_TD_RTMR = ((328, 376), (376, 424), (424, 472), (472, 520))
_TD_REPORT_DATA = (520, 584)

# SGX Report (the QE Report) field offsets.
_QE_MR_ENCLAVE = (64, 96)
_QE_MR_SIGNER = (128, 160)
_QE_REPORT_DATA = (320, 384)


def _slice(blob: bytes, span: tuple[int, int]) -> bytes:
    return blob[span[0]:span[1]]


@dataclass(frozen=True)
class TdxQuote:
    """A parsed Intel TDX Quote v4.

    Only the members a placement decision reads are surfaced. ``mrtd`` is the
    TD's initial measurement and the one this module reports as THE measurement;
    the four ``rtmrs`` are the runtime-extended registers, carried so a policy
    that needs them has them (a permitted-set that is re-attested as it moves is
    roadmap item 469, not this one). ``report_data`` is the 64 bytes the workload
    chose, and the only place a challenge can be bound."""

    version: int
    att_key_type: int
    tee_type: int
    qe_svn: int
    pce_svn: int
    qe_vendor_id: bytes
    user_data: bytes
    tee_tcb_svn: bytes
    mrseam: bytes
    mrsignerseam: bytes
    td_attributes: bytes
    xfam: bytes
    mrtd: bytes
    mrconfigid: bytes
    mrowner: bytes
    mrownerconfig: bytes
    rtmrs: tuple[bytes, bytes, bytes, bytes]
    report_data: bytes
    signed_bytes: bytes
    quote_signature: bytes
    attest_pub_key: bytes
    qe_report: bytes
    qe_report_signature: bytes
    qe_auth_data: bytes
    cert_key_type: int
    cert_data: bytes

    @property
    def qe_report_data(self) -> bytes:
        return _slice(self.qe_report, _QE_REPORT_DATA)

    @property
    def qe_mr_enclave(self) -> bytes:
        return _slice(self.qe_report, _QE_MR_ENCLAVE)

    @property
    def qe_mr_signer(self) -> bytes:
        return _slice(self.qe_report, _QE_MR_SIGNER)


def parse_tdx_quote(blob: Any) -> TdxQuote:
    """Parse a TDX Quote v4 blob, refusing anything that is not one.

    Refusals, each by name: not bytes; shorter than a header plus a body plus the
    signature-data length; a `version` other than 4; a `tee_type` that is SGX or
    unknown; an `attestation_key_type` other than ECDSA-P256; a
    `signature_data_len` that does not account for exactly the bytes present
    (both short and long, so trailing bytes cannot ride along unsigned); a QE
    authentication or certification length that overruns its own structure."""
    if not isinstance(blob, (bytes, bytearray)):
        raise QuoteFormatError(
            f"a TDX quote is bytes, got {type(blob).__name__}")
    quote = bytes(blob)
    if len(quote) < _TDX_MIN_LEN:
        raise QuoteFormatError(
            f"a TDX quote is at least {_TDX_MIN_LEN} bytes (a {_TDX_HEADER_LEN}-byte "
            f"header, a {_TDX_BODY_LEN}-byte TD quote body and a 4-byte signature "
            f"length), got {len(quote)}")
    version, att_key_type = struct.unpack_from("<HH", quote, 0)
    if version != TDX_QUOTE_VERSION:
        raise QuoteFormatError(
            f"unsupported TDX quote version {version}; this root parses version "
            f"{TDX_QUOTE_VERSION} only, and an unknown version is refused rather "
            f"than read with this layout")
    (tee_type,) = struct.unpack_from("<I", quote, 4)
    if tee_type != TDX_TEE_TYPE:
        what = "an SGX quote, whose body is a different structure" if tee_type == SGX_TEE_TYPE else "unknown"
        raise QuoteFormatError(
            f"tee_type is 0x{tee_type:08x} ({what}), expected TDX "
            f"(0x{TDX_TEE_TYPE:08x})")
    if att_key_type != TDX_ATT_KEY_TYPE_ECDSA_P256:
        raise QuoteFormatError(
            f"attestation_key_type is {att_key_type}, expected "
            f"{TDX_ATT_KEY_TYPE_ECDSA_P256} (ECDSA-256-with-P-256)")
    qe_svn, pce_svn = struct.unpack_from("<HH", quote, 8)
    header = quote[:_TDX_HEADER_LEN]
    body = quote[_TDX_HEADER_LEN:_TDX_HEADER_LEN + _TDX_BODY_LEN]
    (sig_len,) = struct.unpack_from("<I", quote, _TDX_HEADER_LEN + _TDX_BODY_LEN)
    present = len(quote) - _TDX_MIN_LEN
    if sig_len != present:
        raise QuoteFormatError(
            f"signature_data_len is {sig_len} but {present} bytes follow it; a "
            f"quote whose declared and actual signature data disagree is refused "
            f"(trailing bytes would otherwise ride along unsigned)")
    sig = quote[_TDX_MIN_LEN:]
    if sig_len < _TDX_SIG_FIXED_LEN + 2:
        raise QuoteFormatError(
            f"the ECDSA-P256 authentication data is at least "
            f"{_TDX_SIG_FIXED_LEN + 2} bytes, got {sig_len}")
    quote_signature = sig[0:64]
    attest_pub_key = sig[64:128]
    qe_report = sig[128:128 + _SGX_REPORT_LEN]
    qe_report_signature = sig[128 + _SGX_REPORT_LEN:_TDX_SIG_FIXED_LEN]
    cursor = _TDX_SIG_FIXED_LEN
    (auth_len,) = struct.unpack_from("<H", sig, cursor)
    cursor += 2
    if cursor + auth_len > sig_len:
        raise QuoteFormatError(
            f"qe_auth_data_size is {auth_len} but only {sig_len - cursor} bytes "
            f"of authentication data remain")
    qe_auth_data = sig[cursor:cursor + auth_len]
    cursor += auth_len
    if cursor + 6 > sig_len:
        raise QuoteFormatError(
            "the certification data header (a 2-byte key type and a 4-byte "
            "length) does not fit in the authentication data that is present")
    (cert_key_type,) = struct.unpack_from("<H", sig, cursor)
    (cert_len,) = struct.unpack_from("<I", sig, cursor + 2)
    cursor += 6
    if cursor + cert_len != sig_len:
        raise QuoteFormatError(
            f"cert_data_size is {cert_len} but {sig_len - cursor} bytes remain; a "
            f"certification-data length that does not close the quote exactly is "
            f"refused")
    cert_data = sig[cursor:cursor + cert_len]
    return TdxQuote(
        version=version,
        att_key_type=att_key_type,
        tee_type=tee_type,
        qe_svn=qe_svn,
        pce_svn=pce_svn,
        qe_vendor_id=quote[12:28],
        user_data=quote[28:48],
        tee_tcb_svn=_slice(body, _TD_TEE_TCB_SVN),
        mrseam=_slice(body, _TD_MRSEAM),
        mrsignerseam=_slice(body, _TD_MRSIGNERSEAM),
        td_attributes=_slice(body, _TD_ATTRIBUTES),
        xfam=_slice(body, _TD_XFAM),
        mrtd=_slice(body, _TD_MRTD),
        mrconfigid=_slice(body, _TD_MRCONFIGID),
        mrowner=_slice(body, _TD_MROWNER),
        mrownerconfig=_slice(body, _TD_MROWNERCONFIG),
        rtmrs=tuple(_slice(body, span) for span in _TD_RTMR),  # type: ignore[arg-type]
        report_data=_slice(body, _TD_REPORT_DATA),
        signed_bytes=header + body,
        quote_signature=quote_signature,
        attest_pub_key=attest_pub_key,
        qe_report=qe_report,
        qe_report_signature=qe_report_signature,
        qe_auth_data=qe_auth_data,
        cert_key_type=cert_key_type,
        cert_data=cert_data,
    )


def _qe_report_binding(attest_pub_key: bytes, qe_auth_data: bytes) -> bytes:
    """What the QE Report's `report_data` must carry: `SHA-256(attestation key ||
    QE authentication data)` in the first 32 bytes, zero in the rest.

    This is the hop that makes the chain a chain. Without it the attestation key
    is an unattached public key the peer supplied, and the platform signature
    over the QE Report would certify nothing about it."""
    return hashlib.sha256(bytes(attest_pub_key) + bytes(qe_auth_data)).digest() + b"\x00" * 32


def build_tdx_quote(*, report_data: bytes, mrtd: bytes,
                    attest_private_key: int, platform_private_key: int,
                    rtmrs: tuple[bytes, bytes, bytes, bytes] | None = None,
                    mrseam: bytes = b"\x00" * 48,
                    mrsignerseam: bytes = b"\x00" * 48,
                    mrconfigid: bytes = b"\x00" * 48,
                    mrowner: bytes = b"\x00" * 48,
                    mrownerconfig: bytes = b"\x00" * 48,
                    tee_tcb_svn: bytes = b"\x00" * 16,
                    td_attributes: bytes = b"\x00" * 8,
                    xfam: bytes = b"\x00" * 8,
                    qe_svn: int = 0, pce_svn: int = 0,
                    qe_vendor_id: bytes = INTEL_QE_VENDOR_ID,
                    user_data: bytes = b"\x00" * 20,
                    qe_auth_data: bytes = b"",
                    qe_mr_enclave: bytes = b"\x11" * 32,
                    qe_mr_signer: bytes = b"\x22" * 32,
                    cert_key_type: int = 5,
                    cert_data: bytes = b"",
                    version: int = TDX_QUOTE_VERSION,
                    tee_type: int = TDX_TEE_TYPE,
                    att_key_type: int = TDX_ATT_KEY_TYPE_ECDSA_P256,
                    qe_report_data: bytes | None = None) -> bytes:
    """The reference attester: assemble a well-formed TDX Quote v4 and sign it.

    This is what a Quoting Enclave does, written out so the parser and the
    verifier can be exercised against the real layout without TDX hardware, and
    so a committed fixture is reproducible from the repository. Every structural
    member is a parameter, because the refusal tests need to build a quote that
    is wrong in exactly one way and correct in every other."""
    def fixed(name: str, value: bytes, width: int) -> bytes:
        raw = bytes(value)
        if len(raw) != width:
            raise QuoteFormatError(f"{name} is {width} bytes, got {len(raw)}")
        return raw

    registers = rtmrs if rtmrs is not None else (b"\x00" * 48,) * 4
    if len(registers) != 4:
        raise QuoteFormatError("a TD carries exactly four RTMRs")
    header = (struct.pack("<HHIHH", version, att_key_type, tee_type, qe_svn, pce_svn)
              + fixed("qe_vendor_id", qe_vendor_id, 16)
              + fixed("user_data", user_data, 20))
    body = (fixed("tee_tcb_svn", tee_tcb_svn, 16)
            + fixed("mrseam", mrseam, 48)
            + fixed("mrsignerseam", mrsignerseam, 48)
            + b"\x00" * 8
            + fixed("td_attributes", td_attributes, 8)
            + fixed("xfam", xfam, 8)
            + fixed("mrtd", mrtd, 48)
            + fixed("mrconfigid", mrconfigid, 48)
            + fixed("mrowner", mrowner, 48)
            + fixed("mrownerconfig", mrownerconfig, 48)
            + b"".join(fixed(f"rtmr{i}", r, 48) for i, r in enumerate(registers))
            + fixed("report_data", report_data, 64))
    if len(header) != _TDX_HEADER_LEN or len(body) != _TDX_BODY_LEN:
        raise QuoteFormatError(
            f"assembled a {len(header)}-byte header and a {len(body)}-byte body, "
            f"expected {_TDX_HEADER_LEN} and {_TDX_BODY_LEN}")
    attest_pub_key = derive_public_key(CURVE_P256, attest_private_key)
    binding = (qe_report_data if qe_report_data is not None
               else _qe_report_binding(attest_pub_key, qe_auth_data))
    # cpu_svn (16) + misc_select (4) + reserved (28) + attributes (16) = 64
    qe_report = (b"\x00" * 64
                 + fixed("qe_mr_enclave", qe_mr_enclave, 32)
                 + b"\x00" * 32
                 + fixed("qe_mr_signer", qe_mr_signer, 32)
                 + b"\x00" * 96
                 + struct.pack("<HH", 0, 0)
                 + b"\x00" * 60
                 + fixed("qe_report_data", binding, 64))
    if len(qe_report) != _SGX_REPORT_LEN:
        raise QuoteFormatError(
            f"assembled a {len(qe_report)}-byte QE report, expected {_SGX_REPORT_LEN}")
    signature_data = (
        ecdsa_sign(CURVE_P256, attest_private_key, header + body)
        + attest_pub_key
        + qe_report
        + ecdsa_sign(CURVE_P256, platform_private_key, qe_report)
        + struct.pack("<H", len(qe_auth_data))
        + bytes(qe_auth_data)
        + struct.pack("<H", cert_key_type)
        + struct.pack("<I", len(cert_data))
        + bytes(cert_data))
    return header + body + struct.pack("<I", len(signature_data)) + signature_data


# --------------------------------------------------------------------------
# AMD SEV-SNP: ATTESTATION_REPORT, ECDSA-P384 with SHA-384
# --------------------------------------------------------------------------

#: `ATTESTATION_REPORT` is a fixed 0x4A0 bytes, signature included.
SEV_SNP_REPORT_SIZE = 0x4A0
#: The signature covers everything BEFORE the signature field.
SEV_SNP_SIGNED_LEN = 0x2A0
#: `signature_algo` 1 is ECDSA P-384 with SHA-384, the only algorithm the
#: specification defines and the only one parsed here.
SEV_SNP_SIG_ALGO_ECDSA_P384 = 1
#: Report versions whose layout is the one below. A version outside this set is
#: refused rather than read with this layout.
SEV_SNP_VERSIONS = (2, 3)
#: Guest policy bit 19: the guest may be debugged, which means its memory is
#: inspectable from the host, which means the enclave guarantee the placement is
#: buying does not hold.
SEV_SNP_POLICY_DEBUG_BIT = 19
#: TDX TDATTRIBUTES bit 0 (TUD.DEBUG): the TD is debuggable, same consequence.
TDX_ATTRIBUTES_DEBUG_BIT = 0

_SEV_REPORT_DATA = (0x050, 0x090)
_SEV_MEASUREMENT = (0x090, 0x0C0)
_SEV_HOST_DATA = (0x0C0, 0x0E0)
_SEV_ID_KEY_DIGEST = (0x0E0, 0x110)
_SEV_AUTHOR_KEY_DIGEST = (0x110, 0x140)
_SEV_REPORT_ID = (0x140, 0x160)
_SEV_CHIP_ID = (0x1A0, 0x1E0)
_SEV_SIGNATURE = (0x2A0, SEV_SNP_REPORT_SIZE)
#: R and S are each 72 bytes, LITTLE-endian, at the start of the signature field.
_SEV_SIG_COMPONENT_LEN = 72


def _sev_encode_signature(signature: bytes) -> bytes:
    """Raw big-endian `R || S` into AMD's 512-byte signature field: each scalar
    little-endian in its own 72-byte slot, the rest zero."""
    width = CURVE_P384.order_len
    r = bytes(signature)[:width][::-1].ljust(_SEV_SIG_COMPONENT_LEN, b"\x00")
    s = bytes(signature)[width:][::-1].ljust(_SEV_SIG_COMPONENT_LEN, b"\x00")
    return (r + s).ljust(_SEV_SIGNATURE[1] - _SEV_SIGNATURE[0], b"\x00")


def _sev_decode_signature(field_bytes: bytes) -> bytes:
    """AMD's 512-byte signature field back to raw big-endian `R || S`.

    The padding is CHECKED, not skipped: a scalar whose little-endian encoding
    spills past the 48 bytes P-384 defines would otherwise be silently truncated
    into a different, valid-looking scalar."""
    width = CURVE_P384.order_len
    r_le = field_bytes[:_SEV_SIG_COMPONENT_LEN]
    s_le = field_bytes[_SEV_SIG_COMPONENT_LEN:2 * _SEV_SIG_COMPONENT_LEN]
    for name, raw in (("R", r_le), ("S", s_le)):
        if any(raw[width:]):
            raise QuoteFormatError(
                f"the report's signature component {name} sets bytes above the "
                f"{width} a P-384 scalar occupies, so it is not a P-384 signature")
    return r_le[:width][::-1] + s_le[:width][::-1]


@dataclass(frozen=True)
class SevSnpReport:
    """A parsed AMD SEV-SNP `ATTESTATION_REPORT`.

    ``measurement`` is the launch measurement of the guest and the one this
    module reports as THE measurement. ``report_data`` is the 64 bytes the guest
    supplied and the only place a challenge can be bound. ``policy`` is the guest
    policy the firmware enforced, and its debug bit is checked rather than
    reported, because a debuggable guest is inspectable from the host."""

    version: int
    guest_svn: int
    policy: int
    family_id: bytes
    image_id: bytes
    vmpl: int
    signature_algo: int
    current_tcb: int
    platform_info: int
    report_data: bytes
    measurement: bytes
    host_data: bytes
    id_key_digest: bytes
    author_key_digest: bytes
    report_id: bytes
    reported_tcb: int
    chip_id: bytes
    signed_bytes: bytes
    signature: bytes

    @property
    def debug_enabled(self) -> bool:
        return bool(self.policy >> SEV_SNP_POLICY_DEBUG_BIT & 1)


def parse_sev_snp_report(blob: Any) -> SevSnpReport:
    """Parse an AMD SEV-SNP attestation report, refusing anything that is not
    one: not bytes, not exactly 0x4A0 bytes, a version outside the layout this
    parser implements, a `signature_algo` other than ECDSA-P384-with-SHA-384, or
    a signature field whose scalars are not P-384 scalars."""
    if not isinstance(blob, (bytes, bytearray)):
        raise QuoteFormatError(
            f"a SEV-SNP attestation report is bytes, got {type(blob).__name__}")
    report = bytes(blob)
    if len(report) != SEV_SNP_REPORT_SIZE:
        raise QuoteFormatError(
            f"a SEV-SNP attestation report is exactly {SEV_SNP_REPORT_SIZE} bytes, "
            f"got {len(report)}")
    version, guest_svn = struct.unpack_from("<II", report, 0)
    if version not in SEV_SNP_VERSIONS:
        raise QuoteFormatError(
            f"unsupported SEV-SNP report version {version}; this root parses "
            f"{', '.join(str(v) for v in SEV_SNP_VERSIONS)} only, and an unknown "
            f"version is refused rather than read with this layout")
    (policy,) = struct.unpack_from("<Q", report, 0x008)
    vmpl, signature_algo = struct.unpack_from("<II", report, 0x030)
    if signature_algo != SEV_SNP_SIG_ALGO_ECDSA_P384:
        raise QuoteFormatError(
            f"signature_algo is {signature_algo}, expected "
            f"{SEV_SNP_SIG_ALGO_ECDSA_P384} (ECDSA P-384 with SHA-384)")
    (current_tcb,) = struct.unpack_from("<Q", report, 0x038)
    (platform_info,) = struct.unpack_from("<Q", report, 0x040)
    (reported_tcb,) = struct.unpack_from("<Q", report, 0x180)
    return SevSnpReport(
        version=version,
        guest_svn=guest_svn,
        policy=policy,
        family_id=report[0x010:0x020],
        image_id=report[0x020:0x030],
        vmpl=vmpl,
        signature_algo=signature_algo,
        current_tcb=current_tcb,
        platform_info=platform_info,
        report_data=_slice(report, _SEV_REPORT_DATA),
        measurement=_slice(report, _SEV_MEASUREMENT),
        host_data=_slice(report, _SEV_HOST_DATA),
        id_key_digest=_slice(report, _SEV_ID_KEY_DIGEST),
        author_key_digest=_slice(report, _SEV_AUTHOR_KEY_DIGEST),
        report_id=_slice(report, _SEV_REPORT_ID),
        reported_tcb=reported_tcb,
        chip_id=_slice(report, _SEV_CHIP_ID),
        signed_bytes=report[:SEV_SNP_SIGNED_LEN],
        signature=_sev_decode_signature(_slice(report, _SEV_SIGNATURE)),
    )


def build_sev_snp_report(*, report_data: bytes, measurement: bytes,
                         vcek_private_key: int, policy: int = 0x30000,
                         version: int = 2, guest_svn: int = 0, vmpl: int = 0,
                         family_id: bytes = b"\x00" * 16,
                         image_id: bytes = b"\x00" * 16,
                         host_data: bytes = b"\x00" * 32,
                         id_key_digest: bytes = b"\x00" * 48,
                         author_key_digest: bytes = b"\x00" * 48,
                         report_id: bytes = b"\x33" * 32,
                         chip_id: bytes = b"\x44" * 64,
                         current_tcb: int = 0, platform_info: int = 0,
                         reported_tcb: int = 0,
                         signature_algo: int = SEV_SNP_SIG_ALGO_ECDSA_P384,
                         signature: bytes | None = None) -> bytes:
    """The reference AMD-SP: assemble a well-formed `ATTESTATION_REPORT` and sign
    it with the VCEK. Present for the same reason :func:`build_tdx_quote` is."""
    def fixed(name: str, value: bytes, width: int) -> bytes:
        raw = bytes(value)
        if len(raw) != width:
            raise QuoteFormatError(f"{name} is {width} bytes, got {len(raw)}")
        return raw

    body = bytearray(SEV_SNP_SIGNED_LEN)
    struct.pack_into("<II", body, 0, version, guest_svn)
    struct.pack_into("<Q", body, 0x008, policy)
    body[0x010:0x020] = fixed("family_id", family_id, 16)
    body[0x020:0x030] = fixed("image_id", image_id, 16)
    struct.pack_into("<II", body, 0x030, vmpl, signature_algo)
    struct.pack_into("<Q", body, 0x038, current_tcb)
    struct.pack_into("<Q", body, 0x040, platform_info)
    body[0x050:0x090] = fixed("report_data", report_data, 64)
    body[0x090:0x0C0] = fixed("measurement", measurement, 48)
    body[0x0C0:0x0E0] = fixed("host_data", host_data, 32)
    body[0x0E0:0x110] = fixed("id_key_digest", id_key_digest, 48)
    body[0x110:0x140] = fixed("author_key_digest", author_key_digest, 48)
    body[0x140:0x160] = fixed("report_id", report_id, 32)
    struct.pack_into("<Q", body, 0x180, reported_tcb)
    body[0x1A0:0x1E0] = fixed("chip_id", chip_id, 64)
    signed = bytes(body)
    raw = signature if signature is not None else ecdsa_sign(
        CURVE_P384, vcek_private_key, signed)
    return signed + _sev_encode_signature(raw)


# --------------------------------------------------------------------------
# The root: what the operator pinned, and how a platform key reaches it
# --------------------------------------------------------------------------

ENDORSEMENT_KIND = "revl.tee-platform-endorsement"
ENDORSEMENT_VERSION = "1.0"
#: Its own domain-separation prefix, on the same discipline as every other
#: signed record in the tree: an endorsement never verifies as a quote, an
#: attestation, an offer or a deploy receipt under a shared key.
ENDORSEMENT_SIGN_DOMAIN = b"revl.tee-platform-endorsement/v1\x00"

VENDOR_INTEL_TDX = "intel-tdx"
VENDOR_AMD_SEV_SNP = "amd-sev-snp"
_VENDORS = (VENDOR_INTEL_TDX, VENDOR_AMD_SEV_SNP)

#: Which curve each quote format's platform key lives on. The verifier reads
#: this table, never the record, so a peer cannot ask for its key bytes to be
#: interpreted on a curve of its choosing.
_PLATFORM_CURVE = {SIGN_ALG_TDX: CURVE_P256, SIGN_ALG_SEV_SNP: CURVE_P384}
_FORMAT_VENDOR = {SIGN_ALG_TDX: VENDOR_INTEL_TDX, SIGN_ALG_SEV_SNP: VENDOR_AMD_SEV_SNP}


@dataclass(frozen=True)
class PinnedKey:
    """A public key the OPERATOR pinned, with the curve it lives on.

    The curve is part of the pin rather than part of the record: reading a key's
    curve out of peer-supplied bytes would let a peer choose how the verifier
    interprets the operator's own configuration."""

    curve: str
    public_key: bytes
    label: str = ""

    def __post_init__(self) -> None:
        if self.curve not in CURVES:
            raise RootError(
                f"a pinned key names curve {self.curve!r}; this root knows "
                f"{', '.join(sorted(CURVES))}")
        try:
            _decode_public_key(CURVES[self.curve], self.public_key)
        except QuoteFormatError as error:
            raise RootError(f"a pinned {self.curve} key is unusable: {error}") from error
        object.__setattr__(self, "public_key", bytes(self.public_key))

    @property
    def key_id(self) -> str:
        return public_key_id(self.public_key)


class AttestationRoot:
    """What a quote's trust chain terminates in.

    ``is_production`` is the whole point of the base class: a caller that needs
    the guarantee asks :func:`require_production_root` rather than inspecting a
    class name, and the one implementation that answers ``False`` says so in
    every verdict it reaches."""

    is_production: bool = False
    label: str = "attestation root"

    def describe(self) -> str:
        return self.label


class HardwareRoot(AttestationRoot):
    """An attestation root outside the peer's control.

    ``platform_keys`` are quote-signing keys pinned directly: the PCK key that
    signs a TDX QE Report, or the VCEK that signs a SEV-SNP report. ``
    endorsement_roots`` are vendor root keys that may CERTIFY a platform key
    through a :class:`PlatformEndorsement`, so an operator can pin one root and
    let the vendor speak for platforms it has never seen.

    Either list may be empty; both may not, because a root that can name no key
    admits nothing and is more likely a configuration mistake than a policy.

    ``permit_debug`` defaults to ``False``: a TD whose TDATTRIBUTES debug bit is
    set, or a SEV-SNP guest whose policy allows debug, is inspectable by the host
    that runs it, so the confidentiality the placement is buying does not hold
    and the quote is refused rather than admitted with a caveat."""

    is_production = True

    def __init__(self, *, platform_keys=(), endorsement_roots=(),
                 permit_debug: bool = False,
                 require_intel_qe_vendor: bool = True,
                 label: str = "pinned hardware attestation root") -> None:
        platforms = tuple(platform_keys)
        roots = tuple(endorsement_roots)
        for pinned in platforms + roots:
            if not isinstance(pinned, PinnedKey):
                raise RootError(
                    f"a root is configured with {type(pinned).__name__}; pin a "
                    f"PinnedKey, so the curve is part of the pin")
        if not platforms and not roots:
            raise RootError(
                "an attestation root with no pinned platform key and no "
                "endorsement root can name no key, so it would refuse every "
                "quote; pin at least one key rather than configuring a root "
                "that decides nothing")
        self.platform_keys: dict[str, PinnedKey] = {p.key_id: p for p in platforms}
        self.endorsement_roots: dict[str, PinnedKey] = {r.key_id: r for r in roots}
        self.permit_debug = bool(permit_debug)
        self.require_intel_qe_vendor = bool(require_intel_qe_vendor)
        self.label = label

    def describe(self) -> str:
        return (f"{self.label} ({len(self.platform_keys)} pinned platform key(s), "
                f"{len(self.endorsement_roots)} endorsement root(s))")

    def resolve_platform_key(self, fingerprint: Any, curve: Curve, *,
                             vendor: str, endorsement: Any = None,
                             now: Optional[datetime] = None
                             ) -> tuple[Optional[bytes], str]:
        """The platform key this root names for ``fingerprint``, or a refusal.

        Two ways in, and NEITHER reads a key out of the quote: a direct pin, or a
        one-hop endorsement signed by a pinned root. A fingerprint the root does
        not know, and an endorsement whose own signer the root does not know, are
        both refusals — "bring your own root" is not a move the peer can make."""
        if not isinstance(fingerprint, str) or not fingerprint:
            return None, ("the evidence names no platform key fingerprint, so the "
                          "root cannot be asked which key signed it")
        pinned = self.platform_keys.get(fingerprint)
        if pinned is not None:
            if pinned.curve != curve.name:
                return None, (f"the platform key pinned as {fingerprint} is a "
                              f"{pinned.curve} key, but this quote format signs on "
                              f"{curve.name}")
            return pinned.public_key, ""
        if endorsement is None:
            return None, (
                f"no attestation root holds a platform key with fingerprint "
                f"{fingerprint}, and the evidence carries no endorsement that a "
                f"pinned root signed; an unrecognised signer is refused rather "
                f"than trusted on its own word")
        ok, reason, key = verify_endorsement(
            endorsement, self, vendor=vendor, curve=curve, now=now)
        if not ok:
            return None, reason
        if public_key_id(key) != fingerprint:
            return None, (
                f"the endorsement certifies platform key "
                f"{public_key_id(key)}, but the evidence names {fingerprint}")
        return key, ""


class DevMacRoot(AttestationRoot):
    """The pre-existing symmetric verifier, kept and LABELLED.

    A MAC proves that the holder of a key authored a record. That is not an
    attestation root: it is a second party the verifier must also trust with a
    secret, and if the peer ever holds that secret the proof is the peer's own
    assertion in a longer spelling. It stays because callers that have only a
    shared secret would otherwise break, and it is fenced three ways:
    ``is_production`` is ``False``, the constructor refuses unless
    ``acknowledged_dev_only=True`` is passed by name, and every verdict reached
    through it carries :data:`DEV_ROOT_NOTE` in its own reason text, so a log
    line cannot be mistaken for a hardware-rooted admission."""

    is_production = False
    label = "development/test symmetric-MAC verifier (NOT an attestation root)"

    def __init__(self, key: Any, *, acknowledged_dev_only: bool = False) -> None:
        if not acknowledged_dev_only:
            raise RootError(
                "the symmetric-MAC verifier is a development and test verifier, "
                "not an attestation root: a peer that holds the key can mint a "
                "proof of a bundle it never ran. Construct it as "
                "DevMacRoot(key, acknowledged_dev_only=True) to say so in the "
                "source, or configure a HardwareRoot for a production placement")
        if not isinstance(key, (bytes, bytearray)) or not key:
            raise RootError("the development verifier's key must be non-empty bytes")
        self.key = bytes(key)


#: Appended to every verdict a :class:`DevMacRoot` reaches, admission included.
DEV_ROOT_NOTE = ("[dev/test symmetric-MAC verifier, not a hardware attestation "
                 "root: this verdict is not evidence against a hostile peer]")


def require_production_root(root: Any) -> AttestationRoot:
    """``root`` if it is a production attestation root; :class:`RootError`
    otherwise. The one call a caller makes to refuse the development verifier
    outright."""
    if not isinstance(root, AttestationRoot):
        raise RootError(
            f"a production attestation root is required, got "
            f"{type(root).__name__}")
    if not root.is_production:
        raise RootError(
            f"{root.describe()} is not a production attestation root; a "
            f"placement that demands an attested TEE needs a root the peer does "
            f"not hold")
    return root


# --------------------------------------------------------------------------
# The one-hop endorsement that stands in for the vendor's own chain
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PlatformEndorsement:
    """A vendor's statement that a platform key is one of its own.

    The same shape the vendor's X.509 chain carries (Intel's PCK chain to the SGX
    Root CA, AMD's VCEK certificate under the ASK and ARK): a subject key, an
    issuer, a validity window, and a signature by a key the verifier pinned.
    Consuming the vendor DER directly is the remaining hop; it changes which
    bytes carry this statement, not where the trust terminates."""

    vendor: str
    curve: str
    platform_key: str
    not_before: str
    not_after: str

    def __post_init__(self) -> None:
        if self.vendor not in _VENDORS:
            raise RootError(
                f"an endorsement names vendor {self.vendor!r}; known vendors are "
                f"{', '.join(repr(v) for v in _VENDORS)}")
        if self.curve not in CURVES:
            raise RootError(f"an endorsement names unknown curve {self.curve!r}")
        try:
            _decode_public_key(CURVES[self.curve], bytes.fromhex(self.platform_key))
        except (QuoteFormatError, ValueError) as error:
            raise RootError(
                f"an endorsement's platform_key is not a usable {self.curve} "
                f"public key: {error}") from error
        for member in ("not_before", "not_after"):
            if _instant(getattr(self, member)) is None:
                raise RootError(
                    f"an endorsement's {member} must be an ISO 8601 instant, got "
                    f"{getattr(self, member)!r}")
        if _instant(self.not_after) <= _instant(self.not_before):
            raise RootError(
                "an endorsement's validity window is empty or backwards, so it "
                "was never valid")

    def body(self) -> dict:
        return {
            "kind": ENDORSEMENT_KIND,
            "version": ENDORSEMENT_VERSION,
            "vendor": self.vendor,
            "curve": self.curve,
            "platform_key": self.platform_key,
            "platform_key_id": public_key_id(bytes.fromhex(self.platform_key)),
            "not_before": self.not_before,
            "not_after": self.not_after,
        }


def _instant(text: Any) -> Optional[datetime]:
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sign_endorsement(endorsement: PlatformEndorsement, root_key: PinnedKey,
                     root_private_key: int) -> dict:
    """Sign an endorsement with a vendor root key. The record records the root's
    fingerprint so a verifier knows which pin to look for, and the fingerprint is
    checked against the key that actually verifies."""
    curve = CURVES[root_key.curve]
    if derive_public_key(curve, root_private_key) != root_key.public_key:
        raise RootError(
            "the private scalar does not derive the pinned root public key")
    body = endorsement.body()
    body["root_curve"] = root_key.curve
    body["root_key_id"] = root_key.key_id
    body["signature"] = ecdsa_sign(
        curve, root_private_key,
        ENDORSEMENT_SIGN_DOMAIN + _canonical_bytes(body)).hex()
    return body


def verify_endorsement(record: Any, root: HardwareRoot, *, vendor: str,
                       curve: Curve, now: Optional[datetime] = None
                       ) -> tuple[bool, str, bytes]:
    """Check an endorsement against the root's pinned vendor keys.

    Returns ``(ok, reason, platform_key)`` and refuses rather than raises. The
    signer is chosen by the root from the record's ``root_key_id``; a
    fingerprint the root does not hold refuses before any signature is checked,
    so an endorsement signed by a key of the peer's own choosing never reaches
    the verify step."""
    if not isinstance(record, Mapping):
        return False, "the platform endorsement is not an object", b""
    for member, expected in (("kind", ENDORSEMENT_KIND),
                             ("version", ENDORSEMENT_VERSION)):
        if record.get(member) != expected:
            return False, (f"the platform endorsement's {member} is "
                           f"{record.get(member)!r}, expected {expected!r}"), b""
    if record.get("vendor") != vendor:
        return False, (f"the platform endorsement is for vendor "
                       f"{record.get('vendor')!r}, but this quote is a "
                       f"{vendor} quote"), b""
    if record.get("curve") != curve.name:
        return False, (f"the platform endorsement certifies a "
                       f"{record.get('curve')!r} key, but this quote format signs "
                       f"on {curve.name}"), b""
    fingerprint = record.get("root_key_id")
    if not isinstance(fingerprint, str) or fingerprint not in root.endorsement_roots:
        return False, (f"the platform endorsement is signed by root key "
                       f"{fingerprint!r}, which this verifier has not pinned; an "
                       f"endorsement is only as good as the root it terminates "
                       f"in, and that root is the verifier's configuration, never "
                       f"the record's"), b""
    pinned = root.endorsement_roots[fingerprint]
    if record.get("root_curve") != pinned.curve:
        return False, (f"the platform endorsement claims its root signs on "
                       f"{record.get('root_curve')!r}, but the pinned root "
                       f"{fingerprint} is a {pinned.curve} key"), b""
    signature = record.get("signature")
    if not isinstance(signature, str):
        return False, "the platform endorsement carries no signature", b""
    try:
        raw_signature = bytes.fromhex(signature)
    except ValueError:
        return False, "the platform endorsement's signature is not hex", b""
    try:
        signed = ENDORSEMENT_SIGN_DOMAIN + _canonical_bytes(
            {k: v for k, v in record.items() if k != "signature"})
    except NotCanonicalizable as error:
        return False, f"the platform endorsement cannot be verified: {error}", b""
    if not ecdsa_verify(CURVES[pinned.curve], pinned.public_key, signed, raw_signature):
        return False, (f"the platform endorsement's signature does not verify "
                       f"under pinned root {fingerprint}: it was signed by "
                       f"another key, or altered after it was issued"), b""
    platform_key_hex = record.get("platform_key")
    if not isinstance(platform_key_hex, str):
        return False, "the platform endorsement names no platform key", b""
    try:
        platform_key = bytes.fromhex(platform_key_hex)
        _decode_public_key(curve, platform_key)
    except (ValueError, QuoteFormatError) as error:
        return False, (f"the endorsed platform key is not a usable {curve.name} "
                       f"public key: {error}"), b""
    if record.get("platform_key_id") != public_key_id(platform_key):
        return False, ("the platform endorsement's platform_key_id does not "
                       "fingerprint the key it carries"), b""
    moment = now if now is not None else datetime.now(timezone.utc)
    not_before = _instant(record.get("not_before"))
    not_after = _instant(record.get("not_after"))
    if not_before is None or not_after is None:
        return False, ("the platform endorsement's validity window is not a pair "
                       "of ISO 8601 instants"), b""
    if moment < not_before:
        return False, (f"the platform endorsement is not valid until "
                       f"{record.get('not_before')}"), b""
    if moment > not_after:
        return False, (f"the platform endorsement expired at "
                       f"{record.get('not_after')}; an expired endorsement is "
                       f"refused rather than honoured on the strength of having "
                       f"once been valid"), b""
    return True, "", platform_key


# --------------------------------------------------------------------------
# The verdict on one quote
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class QuoteVerdict:
    """What a root decided about one quote, and the hardware facts it read.

    ``measurement`` is what the HARDWARE says is running (MRTD for a TD, the
    launch measurement for a SEV-SNP guest), in hex, and it is empty on a
    refusal: a caller that compares it against a permitted set therefore cannot
    accidentally compare a refused quote's measurement and find it acceptable.
    ``report_data`` is the 64 bytes the workload bound into the quote."""

    ok: bool
    reason: str
    format: str = ""
    measurement: str = ""
    report_data: bytes = b""
    platform_key_id: str = ""
    runtime_measurements: tuple[str, ...] = ()
    debug_enabled: bool = False


def _refused(reason: str, fmt: str = "") -> QuoteVerdict:
    return QuoteVerdict(ok=False, reason=reason, format=fmt)


def verify_quote(quote: Any, *, sign_alg: Any, root: Any,
                 expect_report_data: bytes, platform_key_id: Any,
                 endorsement: Any = None,
                 now: Optional[datetime] = None) -> QuoteVerdict:
    """Verify one hardware quote against an attestation root.

    ``expect_report_data`` is the 64 bytes the verifier requires the quote to
    carry: the digest that binds this quote to THIS challenge and THESE claims
    (``tee_attestation.evidence_report_data``). It is compared, not reported,
    because a quote that answers a challenge of the peer's choosing is evidence
    of nothing in particular.

    Returns a :class:`QuoteVerdict` and never raises: a malformed blob is a
    refusal, so a caller iterating candidate peers cannot be broken by one of
    them."""
    if not isinstance(root, HardwareRoot):
        return _refused(
            f"a hardware quote needs a hardware attestation root, got "
            f"{type(root).__name__}; the development symmetric-MAC verifier "
            f"cannot decide a quote and is refused rather than waved through")
    if sign_alg not in QUOTE_SIGN_ALGS:
        return _refused(
            f"unknown quote format {sign_alg!r}; this root verifies "
            f"{', '.join(repr(a) for a in QUOTE_SIGN_ALGS)}")
    if not isinstance(expect_report_data, (bytes, bytearray)) or len(expect_report_data) != 64:
        return _refused(
            "the expected report_data must be exactly 64 bytes, the width both "
            "formats give the workload; without it the quote is bound to no "
            "challenge", sign_alg)
    curve = _PLATFORM_CURVE[sign_alg]
    vendor = _FORMAT_VENDOR[sign_alg]
    platform_key, problem = root.resolve_platform_key(
        platform_key_id, curve, vendor=vendor, endorsement=endorsement, now=now)
    if platform_key is None:
        return _refused(problem, sign_alg)
    if sign_alg == SIGN_ALG_TDX:
        return _verify_tdx(quote, root, platform_key, bytes(expect_report_data),
                           platform_key_id)
    return _verify_sev_snp(quote, root, platform_key, bytes(expect_report_data),
                           platform_key_id)


def _verify_tdx(blob: Any, root: HardwareRoot, platform_key: bytes,
                expect_report_data: bytes, fingerprint: str) -> QuoteVerdict:
    """The TDX chain, in the order that makes each step meaningful: the platform
    key certifies the QE Report, the QE Report certifies the attestation key, the
    attestation key signs the quote, and the quote carries the challenge."""
    try:
        quote = parse_tdx_quote(blob)
    except QuoteFormatError as error:
        return _refused(f"malformed TDX quote: {error}", SIGN_ALG_TDX)
    if root.require_intel_qe_vendor and quote.qe_vendor_id != INTEL_QE_VENDOR_ID:
        return _refused(
            f"the quote's QE vendor id is {quote.qe_vendor_id.hex()}, not Intel's "
            f"({INTEL_QE_VENDOR_ID.hex()})", SIGN_ALG_TDX)
    if not ecdsa_verify(CURVE_P256, platform_key, quote.qe_report,
                        quote.qe_report_signature):
        return _refused(
            f"the QE Report is not signed by the platform key pinned as "
            f"{fingerprint}: the quote was issued by a platform this root does "
            f"not endorse, or altered after it was issued", SIGN_ALG_TDX)
    expected_binding = _qe_report_binding(quote.attest_pub_key, quote.qe_auth_data)
    if not hmac.compare_digest(quote.qe_report_data, expected_binding):
        return _refused(
            "the QE Report does not bind this attestation key: its report_data is "
            "not SHA-256(attestation key || QE authentication data), so the "
            "platform signature certifies some other key and the attestation key "
            "is unattached", SIGN_ALG_TDX)
    try:
        _decode_public_key(CURVE_P256, quote.attest_pub_key)
    except QuoteFormatError as error:
        return _refused(f"the quote's attestation key is unusable: {error}",
                        SIGN_ALG_TDX)
    if not ecdsa_verify(CURVE_P256, quote.attest_pub_key, quote.signed_bytes,
                        quote.quote_signature):
        return _refused(
            "the quote body is not signed by the attestation key the QE Report "
            "certifies: the header or the TD quote body was altered after the "
            "quote was issued", SIGN_ALG_TDX)
    debug = bool(quote.td_attributes[0] >> TDX_ATTRIBUTES_DEBUG_BIT & 1)
    if debug and not root.permit_debug:
        return _refused(
            "the TD's TDATTRIBUTES debug bit is set, so the TD is inspectable "
            "from the host that runs it and the confidentiality this placement "
            "demands does not hold", SIGN_ALG_TDX)
    if not hmac.compare_digest(quote.report_data, expect_report_data):
        return _refused(
            "the quote's report_data does not match this placement's challenge "
            "digest: the quote answers a different challenge, or covers different "
            "claims, so it is a valid quote about something else", SIGN_ALG_TDX)
    return QuoteVerdict(
        ok=True,
        reason=(f"the TDX quote is signed by an attestation key the platform key "
                f"pinned as {fingerprint} certifies, and answers this challenge"),
        format=SIGN_ALG_TDX,
        measurement=quote.mrtd.hex(),
        report_data=quote.report_data,
        platform_key_id=fingerprint,
        runtime_measurements=tuple(r.hex() for r in quote.rtmrs),
        debug_enabled=debug,
    )


def _verify_sev_snp(blob: Any, root: HardwareRoot, platform_key: bytes,
                    expect_report_data: bytes, fingerprint: str) -> QuoteVerdict:
    """The SEV-SNP chain: the VCEK signs the report directly, so the hops are the
    signature, the guest policy, and the challenge."""
    try:
        report = parse_sev_snp_report(blob)
    except QuoteFormatError as error:
        return _refused(f"malformed SEV-SNP attestation report: {error}",
                        SIGN_ALG_SEV_SNP)
    if not ecdsa_verify(CURVE_P384, platform_key, report.signed_bytes,
                        report.signature):
        return _refused(
            f"the attestation report is not signed by the VCEK pinned as "
            f"{fingerprint}: the report came from a platform this root does not "
            f"endorse, or was altered after it was signed", SIGN_ALG_SEV_SNP)
    if report.debug_enabled and not root.permit_debug:
        return _refused(
            f"the guest policy (0x{report.policy:x}) allows debug, so the guest's "
            f"memory is inspectable from the host and the confidentiality this "
            f"placement demands does not hold", SIGN_ALG_SEV_SNP)
    if not hmac.compare_digest(report.report_data, expect_report_data):
        return _refused(
            "the report's report_data does not match this placement's challenge "
            "digest: the report answers a different challenge, or covers "
            "different claims, so it is a valid report about something else",
            SIGN_ALG_SEV_SNP)
    return QuoteVerdict(
        ok=True,
        reason=(f"the SEV-SNP attestation report is signed by the VCEK pinned as "
                f"{fingerprint}, and answers this challenge"),
        format=SIGN_ALG_SEV_SNP,
        measurement=report.measurement.hex(),
        report_data=report.report_data,
        platform_key_id=fingerprint,
        debug_enabled=report.debug_enabled,
    )

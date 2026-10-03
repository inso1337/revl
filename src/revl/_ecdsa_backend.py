"""The optional ECDSA signing backend: `cryptography`, when it is installed
(issue #1460).

Why this exists
---------------
`tee_quote` signs in pure Python. Its scalar multiplication is double-and-add,
so the time one signature takes depends on the bits of the RFC 6979 nonce `k`.
Nonce bits leaking through timing is a known key-recovery route: lattice attacks
recover the private key from a modest number of timed signatures. Verification
has no such problem (every input to it is public), so it stays pure Python.

Signing goes through OpenSSL, by way of `cryptography`, whenever the library is
importable and supports deterministic ECDSA. `revl` keeps zero required runtime
dependencies: the library is the optional extra ``revl[crypto]``.

Byte compatibility
------------------
Both backends use RFC 6979 deterministic nonces with the curve's own hash, so
the same key and message produce the same `R || S` bytes on either one. That is
what keeps every committed fixture reproducible and every pinned digest stable
whichever backend a machine has. The backend is only used when it can keep that
promise: an installed `cryptography` that cannot sign deterministically (older
than 44, or linked against an OpenSSL without RFC 6979) is treated as absent,
and the probe says why.

Nothing here decides WHEN signing must not fall back to the pure path. That is
the caller's knowledge (`tee_quote.ecdsa_sign(..., network_exposed=True)`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

#: The optional extra that installs this backend, and the command that does it.
#: Every refusal names both, so the fix is in the message.
EXTRA = "revl[crypto]"
INSTALL_HINT = "pip install 'revl[crypto]'"

#: The backend's name as :func:`revl.tee_quote.signing_backend` reports it.
NAME = "cryptography"


class SigningBackendUnavailable(RuntimeError):
    """A signing path that is exposed to a network was asked to sign and the
    constant-time backend is not available. The message names the extra."""


@dataclass(frozen=True)
class Probe:
    """Whether the backend can sign, and if not, why not."""

    available: bool
    reason: str
    version: str = ""


_probe: Optional[Probe] = None


def reset() -> None:
    """Forget the cached probe. For tests that change what is importable."""
    global _probe
    _probe = None


def probe() -> Probe:
    """Can `cryptography` sign deterministically here? Cached after the first
    answer, because what is importable does not change under a running
    process (tests that simulate it call :func:`reset`)."""
    global _probe
    if _probe is None:
        _probe = _probe_now()
    return _probe


def _probe_now() -> Probe:
    try:
        import cryptography  # noqa: PLC0415
        from cryptography.hazmat.primitives import hashes  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric import ec  # noqa: PLC0415
    except ImportError as error:
        return Probe(False, f"`cryptography` is not installed ({error})")
    version = str(getattr(cryptography, "__version__", "unknown"))
    try:
        key = ec.derive_private_key(1, ec.SECP256R1())
        key.sign(b"probe", ec.ECDSA(hashes.SHA256(), deterministic_signing=True))
    except TypeError:
        return Probe(False, f"`cryptography` {version} predates deterministic "
                            f"ECDSA (RFC 6979 needs 44 or later)", version)
    except Exception as error:  # noqa: BLE001 - UnsupportedAlgorithm and kin
        return Probe(False, f"`cryptography` {version} cannot sign "
                            f"deterministically on this OpenSSL "
                            f"({type(error).__name__}: {error})", version)
    return Probe(True, f"`cryptography` {version}", version)


def _curve_and_hash(curve: Any) -> Optional[tuple[Any, Any]]:
    """The `cryptography` curve and hash objects for a `tee_quote.Curve`, or
    ``None`` when this backend does not implement that exact pair.

    The curve is matched on EVERY parameter, not on its name: a `Curve` rebuilt
    with `dataclasses.replace` keeps its name, and signing it as the named curve
    would sign on a different group than the caller asked for. An unmatched
    curve or digest is left to the pure path, which implements whatever it is
    given."""
    from cryptography.hazmat.primitives import hashes  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: PLC0415

    from .tee_quote import CURVE_P256, CURVE_P384  # noqa: PLC0415

    def params(c: Any) -> tuple:
        return (c.p, c.b, c.gx, c.gy, c.n)

    groups = {params(CURVE_P256): ec.SECP256R1, params(CURVE_P384): ec.SECP384R1}
    digests = {"sha1": hashes.SHA1, "sha224": hashes.SHA224,
               "sha256": hashes.SHA256, "sha384": hashes.SHA384,
               "sha512": hashes.SHA512}
    group = groups.get(params(curve))
    digest = digests.get(str(curve.digest).lower())
    if group is None or digest is None:
        return None
    return group(), digest()


def supports(curve: Any) -> bool:
    """Is the backend available AND does it implement this curve and hash?"""
    return probe().available and _curve_and_hash(curve) is not None


def _private_key(curve: Any, private_key: int):
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: PLC0415

    pair = _curve_and_hash(curve)
    if pair is None:  # callers check `supports` first
        raise SigningBackendUnavailable(
            f"the {NAME} backend does not implement {curve.name} with "
            f"{curve.digest}")
    return ec.derive_private_key(private_key, pair[0]), pair[1]


def sign(curve: Any, private_key: int, message: bytes) -> bytes:
    """Deterministic ECDSA (RFC 6979) through OpenSSL, as raw `R || S`: the same
    bytes `tee_quote`'s pure signer produces for the same inputs."""
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric.utils import (  # noqa: PLC0415
        decode_dss_signature,
    )

    key, digest = _private_key(curve, private_key)
    der = key.sign(bytes(message),
                   ec.ECDSA(digest, deterministic_signing=True))
    r, s = decode_dss_signature(der)
    width = curve.order_len
    return r.to_bytes(width, "big") + s.to_bytes(width, "big")


def public_key(curve: Any, private_key: int) -> bytes:
    """The raw `X || Y` public key for a private scalar, derived by OpenSSL, so
    the one scalar multiplication by the SECRET key is not a Python loop
    either."""
    key, _ = _private_key(curve, private_key)
    numbers = key.public_key().public_numbers()
    width = curve.coord_len
    return numbers.x.to_bytes(width, "big") + numbers.y.to_bytes(width, "big")

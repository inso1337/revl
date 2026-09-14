"""`tee_quote`'s ECDSA against OpenSSL's, on inputs neither of them chose.

Committed vectors (`tests/test_ecdsa_vectors.py`) cover the cases somebody
thought to write down. This file covers the rest of the input space the only way
a test can: generate keys and messages at random, and require that a second,
independent implementation agrees about every one of them, in both directions.

  * every signature `ecdsa_sign` produces must verify under OpenSSL;
  * every signature OpenSSL produces must verify under `ecdsa_verify`;
  * the two must agree about mutated signatures as well, which is the half that
    would catch an over-permissive verifier rather than a broken signer.

`cryptography` is a HARD import and a declared test dependency, not an
`importorskip`. The repository has been bitten twice by a suite that was correct,
gated on something nobody installed, and therefore reported SKIPPED forever
(item 430, item 433, issue #266). A differential test that vanishes when the
library is absent is exactly that shape, and it would vanish precisely on the
machines least likely to have a crypto library installed.
`test_this_differential_cannot_skip_itself` is the guard, and CI installs the
library through the `test` extra in pyproject.toml like every other test
dependency.

Deterministic inputs: the random draws come from a seeded `random.Random`, so a
disagreement is reproducible from the file rather than from a lucky run. OpenSSL
picks its own nonce, so the signatures it contributes are genuinely fresh on
every run even though the keys and messages are not.
"""

from __future__ import annotations

import ast
import dataclasses
import random
import re
from pathlib import Path

import pytest

# Hard, deliberately. See the module docstring.
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils

from revl.tee_quote import (
    CURVE_P256,
    CURVE_P384,
    Curve,
    QuoteFormatError,
    derive_public_key,
    ecdsa_sign,
    ecdsa_verify,
)

ROOT = Path(__file__).resolve().parent.parent

LIBRARY_CURVE = {"P-256": ec.SECP256R1, "P-384": ec.SECP384R1}
LIBRARY_HASH = {
    "sha256": hashes.SHA256,
    "sha384": hashes.SHA384,
    "sha512": hashes.SHA512,
}

#: (curve, digest) pairings to cross-check. The first two are what the TDX and
#: SEV-SNP quote formats actually use. The third and fourth are the widths the
#: matched pairings never reach: a digest WIDER than the group order, which must
#: be cut to the leftmost `qlen` bits, and a digest NARROWER than it, which must
#: not be. Wycheproof ships no P1363 suite for P-384 with SHA-256, so the narrow
#: case has no committed vectors and this differential is its only coverage.
PAIRINGS = [
    (CURVE_P256, "sha256"),
    (CURVE_P384, "sha384"),
    (CURVE_P256, "sha512"),
    (CURVE_P384, "sha256"),
]
PAIRING_IDS = [f"{curve.name}/{digest}" for curve, digest in PAIRINGS]

#: Random key/message draws per pairing. Pure-Python P-384 arithmetic is the
#: cost here; 16 keeps the whole file inside a few seconds while still putting
#: 64 independent keys and 128 signatures through both implementations.
ROUNDS = 16

#: Bumped by every cross-check performed, and asserted at the end. A
#: differential that silently stops comparing is a differential that passes.
COMPARISONS: dict[str, int] = {}


def _tally(name: str, count: int = 1) -> None:
    COMPARISONS[name] = COMPARISONS.get(name, 0) + count


def _library_key(curve: Curve, scalar: int):
    return ec.derive_private_key(scalar, LIBRARY_CURVE[curve.name]())


def _library_public(curve: Curve, public_key: bytes):
    width = curve.coord_len
    numbers = ec.EllipticCurvePublicNumbers(
        int.from_bytes(public_key[:width], "big"),
        int.from_bytes(public_key[width:], "big"),
        LIBRARY_CURVE[curve.name](),
    )
    return numbers.public_key()


def _algorithm(digest: str):
    return ec.ECDSA(LIBRARY_HASH[digest]())


def _to_der(curve: Curve, signature: bytes) -> bytes:
    width = curve.order_len
    return asym_utils.encode_dss_signature(
        int.from_bytes(signature[:width], "big"),
        int.from_bytes(signature[width:], "big"))


def _from_der(curve: Curve, der: bytes) -> bytes:
    r, s = asym_utils.decode_dss_signature(der)
    width = curve.order_len
    return r.to_bytes(width, "big") + s.to_bytes(width, "big")


def _library_verifies(public, der: bytes, message: bytes, digest: str) -> bool:
    try:
        public.verify(der, message, _algorithm(digest))
    except (InvalidSignature, ValueError):
        return False
    return True


def _draws(curve: Curve, seed: int):
    """Deterministic (scalar, message) pairs inside the valid scalar range."""
    rng = random.Random(seed)
    for index in range(ROUNDS):
        scalar = rng.randrange(1, curve.n)
        length = rng.choice([0, 1, 31, 32, 33, 64, 200])
        yield scalar, rng.randbytes(length), index


# --------------------------------------------------------------------------


@pytest.mark.parametrize("curve,digest", PAIRINGS, ids=PAIRING_IDS)
def test_public_keys_agree(curve: Curve, digest: str) -> None:
    """`derive_public_key` lands on the point OpenSSL lands on.

    Every other check in this file would still pass if both implementations
    were handed the same wrong point, so this one goes first."""
    for scalar, _, _ in _draws(curve, seed=0x5EED + index_seed(curve.name)):
        mine = derive_public_key(curve, scalar)
        numbers = _library_key(curve, scalar).public_key().public_numbers()
        width = curve.coord_len
        expected = (numbers.x.to_bytes(width, "big")
                    + numbers.y.to_bytes(width, "big"))
        assert mine == expected, (
            f"{curve.name}: derive_public_key disagrees with OpenSSL for "
            f"scalar {scalar:x}"
        )
        _tally("public keys")


@pytest.mark.parametrize("curve,digest", PAIRINGS, ids=PAIRING_IDS)
def test_every_signature_this_module_makes_verifies_under_openssl(
        curve: Curve, digest: str) -> None:
    """The signer, checked by somebody else.

    `ecdsa_sign` is deterministic (RFC 6979), so a defect in it is a defect that
    reproduces. It is also the function `tests/test_tee_quote.py` uses to build
    the fixtures it then verifies, which is why a round trip inside this module
    proves so little on its own."""
    bound = dataclasses.replace(curve, digest=digest)
    for scalar, message, index in _draws(curve, seed=0xA11CE + index_seed(digest)):
        public = _library_public(curve, derive_public_key(curve, scalar))
        signature = ecdsa_sign(bound, scalar, message)
        assert len(signature) == 2 * curve.order_len
        assert _library_verifies(public, _to_der(curve, signature), message,
                                 digest), (
            f"{curve.name}/{digest} round {index}: OpenSSL rejects a signature "
            f"ecdsa_sign produced over {message.hex()} with scalar {scalar:x}"
        )
        _tally("mine verified by openssl")


@pytest.mark.parametrize("curve,digest", PAIRINGS, ids=PAIRING_IDS)
def test_every_signature_openssl_makes_verifies_here(curve: Curve,
                                                     digest: str) -> None:
    """The verifier, fed signatures it did not produce.

    OpenSSL chooses its nonce at random, so these are fresh values on every run
    and they sweep the r and s space in a way a deterministic signer cannot."""
    bound = dataclasses.replace(curve, digest=digest)
    for scalar, message, index in _draws(curve, seed=0xB0B + index_seed(digest)):
        private = _library_key(curve, scalar)
        public_key = derive_public_key(curve, scalar)
        for _ in range(2):
            der = private.sign(message, _algorithm(digest))
            signature = _from_der(curve, der)
            assert ecdsa_verify(bound, public_key, message, signature) is True, (
                f"{curve.name}/{digest} round {index}: ecdsa_verify rejects a "
                f"signature OpenSSL produced over {message.hex()}"
            )
            _tally("openssl verified by mine")


@pytest.mark.parametrize("curve,digest", PAIRINGS, ids=PAIRING_IDS)
def test_the_two_agree_about_signatures_that_are_wrong(curve: Curve,
                                                       digest: str) -> None:
    """Agreement on rejection, which is the direction that matters.

    A verifier that accepts everything passes every positive test ever written.
    Each round takes a valid signature and mutates it the ways an attacker
    would: a flipped bit in r, a flipped bit in s, the scalars swapped, the
    message changed underneath it, and the signature moved to another key."""
    bound = dataclasses.replace(curve, digest=digest)
    width = curve.order_len
    disagreements: list[str] = []
    for scalar, message, index in _draws(curve, seed=0xDEAD + index_seed(digest)):
        public_key = derive_public_key(curve, scalar)
        signature = ecdsa_sign(bound, scalar, message)
        r = int.from_bytes(signature[:width], "big")
        s = int.from_bytes(signature[width:], "big")

        other_scalar = (scalar % (curve.n - 1)) + 1
        if other_scalar == scalar:
            other_scalar = (other_scalar % (curve.n - 1)) + 1
        other_key = derive_public_key(curve, other_scalar)

        candidates = [
            ("bit flipped in r", public_key, message, (r ^ 1, s)),
            ("bit flipped in s", public_key, message, (r, s ^ 1)),
            ("high bit flipped in r", public_key, message,
             (r ^ (1 << 200), s)),
            ("r and s swapped", public_key, message, (s, r)),
            ("message changed", public_key, message + b"\x00", (r, s)),
            ("other key", other_key, message, (r, s)),
            ("r zeroed", public_key, message, (0, s)),
            ("s zeroed", public_key, message, (r, 0)),
            ("r at the order", public_key, message, (curve.n, s)),
            ("s at the order", public_key, message, (r, curve.n)),
        ]
        for name, key, msg, (rr, ss) in candidates:
            mine = ecdsa_verify(bound, key, msg,
                                rr.to_bytes(width, "big")
                                + ss.to_bytes(width, "big")) if (
                0 <= rr < (1 << (8 * width)) and 0 <= ss < (1 << (8 * width))
            ) else False
            try:
                der = asym_utils.encode_dss_signature(rr, ss)
            except ValueError:
                theirs = False
            else:
                theirs = _library_verifies(
                    _library_public(curve, key), der, msg, digest)
            _tally("rejection agreement")
            if mine is not theirs:
                disagreements.append(
                    f"{curve.name}/{digest} round {index} [{name}]: "
                    f"ecdsa_verify says {mine}, OpenSSL says {theirs} "
                    f"(r={rr:x} s={ss:x})")
    assert not disagreements, (
        f"{len(disagreements)} verdict disagreements with OpenSSL:\n  "
        + "\n  ".join(disagreements[:20]))


@pytest.mark.parametrize("curve,digest", PAIRINGS, ids=PAIRING_IDS)
def test_openssl_and_this_module_reject_the_same_off_curve_key(
        curve: Curve, digest: str) -> None:
    """Neither will do arithmetic in a group the peer picked.

    The two REFUSE differently, which is deliberate on this side: `ecdsa_verify`
    raises for a bad public key because that is the VERIFIER's own pinned
    configuration, and returns False for a bad signature because that is the
    peer's record. Both are refusals; only one is a peer's move."""
    bound = dataclasses.replace(curve, digest=digest)
    scalar = 0x2024
    public_key = derive_public_key(curve, scalar)
    width = curve.coord_len
    x = int.from_bytes(public_key[:width], "big")
    y = (int.from_bytes(public_key[width:], "big") + 1) % curve.p
    tampered = x.to_bytes(width, "big") + y.to_bytes(width, "big")

    with pytest.raises(QuoteFormatError):
        ecdsa_verify(bound, tampered, b"anything", bytes(2 * curve.order_len))

    with pytest.raises(ValueError):
        ec.EllipticCurvePublicNumbers(
            x, y, LIBRARY_CURVE[curve.name]()).public_key()
    _tally("off-curve refusals")


def index_seed(digest: str) -> int:
    """A stable per-digest offset, so the four pairings do not all draw the same
    scalars. `hash()` is salted per process and would make a failure
    irreproducible, so the digest name is folded by hand."""
    return sum(ord(character) for character in digest)


# --------------------------------------------------------------------------
# The anti-inertia half
# --------------------------------------------------------------------------


def test_this_differential_is_not_vacuous():
    """Every comparison this file claims to make, counted.

    Run last by declaration order inside the module, which pytest preserves, so
    the tallies above are already in. If a parametrisation is dropped or a loop
    stops iterating, the numbers fall and this reddens rather than the suite
    quietly getting cheaper."""
    expected_minimum = {
        "public keys": len(PAIRINGS) * ROUNDS,
        "mine verified by openssl": len(PAIRINGS) * ROUNDS,
        "openssl verified by mine": len(PAIRINGS) * ROUNDS * 2,
        "rejection agreement": len(PAIRINGS) * ROUNDS * 10,
        "off-curve refusals": len(PAIRINGS),
    }
    shortfall = {
        name: (COMPARISONS.get(name, 0), floor)
        for name, floor in expected_minimum.items()
        if COMPARISONS.get(name, 0) < floor
    }
    assert not shortfall, (
        "the differential ran fewer comparisons than it declares: "
        f"{shortfall} (got, expected at least). Either a test above did not "
        "run, or it stopped comparing."
    )


def test_this_differential_cannot_skip_itself():
    """No `skipif`, no `importorskip`, nowhere in this file.

    A differential test that disappears when the library it differs against is
    missing provides its coverage exactly where the coverage is least needed.
    The import is hard; keep it hard, and keep `cryptography` in the `test`
    extra so `pip install -e '.[test]'` is enough to collect this file.

    This mirrors `tests/test_env_gated_skips_run_somewhere.py
    ::test_this_guard_cannot_itself_skip`, which is where the rule comes from."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    banned = {"importorskip", "skip", "xfail", "skipif"}
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in banned:
            used.add(node.attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in banned:
            used.add(node.func.id)
    assert not used, (
        f"this file uses {sorted(used)}, so its checks can report SKIPPED, "
        "which is the same colour as a pass. Make the dependency hard."
    )

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r"^test = \[[^\]]*cryptography", pyproject, re.MULTILINE), (
        "`cryptography` is imported at the top of this file but is not in the "
        "`test` extra, so a plain `pip install -e '.[test]'` job cannot collect "
        "it and the only differential check on the quote verifier's crypto "
        "stops running."
    )

    # The `frontend-cordis` job runs this whole suite out of
    # backends/python/.venv, which is built by setup.sh from a LITERAL package
    # list rather than from the `test` extra. A dependency added to one and not
    # the other errors that job instead of running here; the setup script says
    # to keep the two in step, so this is the thing that keeps them.
    setup = (ROOT / "backends" / "python" / "setup.sh").read_text(
        encoding="utf-8")
    assert "cryptography" in setup, (
        "backends/python/setup.sh does not install `cryptography`, so the "
        "`frontend-cordis` job (which runs the whole root suite out of that "
        "venv) cannot collect this file. Add it to the literal package list "
        "there as well as to the `test` extra."
    )


def test_the_library_under_test_is_not_the_library_being_tested():
    """`src/revl/tee_quote.py` must not have grown an import of `cryptography`.

    The point of the module is that `revl` has no runtime dependencies, and the
    point of this file is that a second implementation checks the first. Both
    stop being true in the same instant if the module ever imports the library,
    and the differential would then be comparing OpenSSL against itself and
    passing forever."""
    source = (ROOT / "src" / "revl" / "tee_quote.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert name.split(".")[0] not in {"cryptography", "ecdsa", "nacl"}, (
                f"tee_quote.py imports {name}. It is meant to be the pure-Python "
                "implementation this file differs OpenSSL against, and revl ships "
                "with no runtime dependencies."
            )

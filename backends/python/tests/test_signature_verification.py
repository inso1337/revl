"""Known-answer verification for Ed25519 and RSA RS256 with `cryptography`.

`cryptography` is a declared backend dependency, installed from
`backends/python/requirements.txt` under `--require-hashes`, so it is imported
HARD here: no `importorskip`, no skip conditions. Each algorithm has exactly
one committed vector (public key, message, signature), generated once and
never regenerated in the test, plus a tampered-message rejection.

RS256 is RSASSA-PKCS1-v1_5 with SHA-256, per RFC 8017 section 8.2.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

BACKEND = Path(__file__).resolve().parents[1]

MESSAGE = b"revl signature verification known-answer vector 3008"
TAMPERED_MESSAGE = MESSAGE + b"!"

ED25519_PUBLIC_KEY_PEM = b"""\
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAA6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=
-----END PUBLIC KEY-----
"""
ED25519_SIGNATURE = base64.b64decode(
    "9xPX/sRbU/JC6Ihuv9QRlVYdSq3+EAnx2SSWIVYmVYDoeKxPah/ZPIvHEXygH3gH"
    "AjctydhGu7YEQ198dGGaBA=="
)

RSA_PUBLIC_KEY_PEM = b"""\
-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAwxehU3pqqmek0A3uJyqA
TxEIIpixjZ49EEGveZsDDjgdZUuhoBOYzuGdrQbqkiDlKKbeKCHmZbnlMXWt9tc3
BE/5qu0jnj8ZsEFgXFfLbT1d7FE0P3RKb3sA5joj+diPSak/Nl7cTzfxYlDKUCvE
WksMxWCNWarcHrZ1JuXK/ZoDqR2mPSqZXVxlqujb17e7lopHITJWkwRSGHSWB5pb
pBAd1baJdl4MvKuA+Naj5DgR30oeTxTFQ57hPohYY3BoZ82BGlW8pwtrTPpICtPw
71EUfxfj2SOw8LboYylDIO+qGba1Gpj+aJfWi1engS7x6qflPv8pDkTajXSguQlF
xQIDAQAB
-----END PUBLIC KEY-----
"""
RSA_SIGNATURE = base64.b64decode(
    "ZEcPWixFm5V4EDA6zZyOHmnWBnuNkQNRF+u+EuSx8fIy6erVNzsyyrdDGvKGUUJu"
    "5Y9o0396FcxzPflTeDDjOO8/wl9LRhXPejCC7TmNfPglyoYEswRMh9O1gw+gVDA+"
    "AYH9oujSXHF0efkM75+jntHmfl18qW9ifnRaR6yULJsTOyosbHCGJSsDKHFAomI9"
    "cCmeIQeNsGUnT8wdv4VSVko0ibjd7SLRaK22n03hB74FFAcJ14OGDTKExhPNr2jI"
    "tzP/HLvHYclWeO0+iSEVvY2P7OVE4DwgnE6/BjSFTTpcu4QxSjZ7/NSCw2oB2GZ6"
    "98ySTO4xGMFmbuf4jyiVdA=="
)


def test_ed25519_known_answer_verifies():
    public_key = serialization.load_pem_public_key(ED25519_PUBLIC_KEY_PEM)
    assert isinstance(public_key, ed25519.Ed25519PublicKey)
    public_key.verify(ED25519_SIGNATURE, MESSAGE)


def test_ed25519_tampered_message_is_rejected():
    public_key = serialization.load_pem_public_key(ED25519_PUBLIC_KEY_PEM)
    with pytest.raises(InvalidSignature):
        public_key.verify(ED25519_SIGNATURE, TAMPERED_MESSAGE)


def test_rsa_rs256_known_answer_verifies():
    public_key = serialization.load_pem_public_key(RSA_PUBLIC_KEY_PEM)
    assert isinstance(public_key, rsa.RSAPublicKey)
    public_key.verify(
        RSA_SIGNATURE, MESSAGE, padding.PKCS1v15(), hashes.SHA256()
    )


def test_rsa_rs256_tampered_message_is_rejected():
    public_key = serialization.load_pem_public_key(RSA_PUBLIC_KEY_PEM)
    with pytest.raises(InvalidSignature):
        public_key.verify(
            RSA_SIGNATURE, TAMPERED_MESSAGE, padding.PKCS1v15(), hashes.SHA256()
        )


def _lock_blocks(lock: str) -> list[str]:
    """One string per requirement entry, including its indented hash lines."""
    blocks: list[str] = []
    current: list[str] = []
    for line in lock.splitlines():
        if line.startswith(("#", " ")) or not line.strip():
            if current:
                current.append(line)
            continue
        if current:
            blocks.append("\n".join(current))
        current = [line]
    if current:
        blocks.append("\n".join(current))
    return blocks


def test_backend_lock_pins_cryptography_by_version_and_hash():
    lock = (BACKEND / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"(?m)^cryptography==50\.0\.2\s*\\?$", lock), (
        "backends/python/requirements.txt must pin cryptography==50.0.2"
    )
    blocks = _lock_blocks(lock)
    assert blocks, "the lock has no requirements"
    missing = [b.splitlines()[0] for b in blocks if "--hash=sha256:" not in b]
    assert not missing, f"unhashed entries in the lock: {missing}"
    crypto = [b for b in blocks if b.startswith("cryptography==")]
    assert len(crypto) == 1
    hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", crypto[0])
    assert len(hashes) >= 20, (
        "the cryptography entry carries too few wheel hashes to cover the "
        f"supported platforms: {len(hashes)}"
    )


def test_setup_installs_the_hashed_lock_before_the_test_dependencies():
    lines = (BACKEND / "setup.sh").read_text(encoding="utf-8").splitlines()
    hashed = [
        i for i, line in enumerate(lines)
        if line.startswith("uv pip install")
        and "--require-hashes -r requirements.txt" in line
    ]
    assert len(hashed) == 1, (
        "backends/python/setup.sh must install the hashed lock exactly once"
    )
    test_deps = [
        i for i, line in enumerate(lines)
        if line.startswith("uv pip install") and "pytest" in line
    ]
    assert len(test_deps) == 1, (
        "backends/python/setup.sh must install the test dependencies exactly once"
    )
    assert hashed[0] < test_deps[0], (
        "the hashed lock must be installed before the test dependencies"
    )
    block = []
    line_no = test_deps[0]
    while line_no < len(lines):
        block.append(lines[line_no])
        if not lines[line_no].rstrip().endswith("\\"):
            break
        line_no += 1
    assert "cryptography" not in "\n".join(block), (
        "cryptography belongs in the hashed lock, not the test dependency line"
    )

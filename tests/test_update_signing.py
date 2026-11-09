"""Release-manifest signing: pinning, windows, refusals and the two commands.

The tests use ``identity_from_seed`` so a fixed digest can be pinned, and they
touch no network and no MCP runtime. The tool round trip runs the script under
the same interpreter, because that is how a maintainer invokes it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from revl.peer_identity import (
    identity_from_seed,
    load_private_identity,
    sign_record,
)
from revl.update.errors import UpdateError
from revl.update.signing import (RELEASE_DOMAIN, require_signed,
                                 verify_manifest)

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "release_signing.py"
SEED = b"revl release signing fixture, not a production key"
IDENTITY = identity_from_seed("revl-release", SEED)
NOT_BEFORE = "2026-01-01T00:00:00Z"
NOT_AFTER = "2027-01-01T00:00:00Z"
NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _document() -> dict:
    return {
        "schema": "revl.release/1",
        "project": "revl",
        "version": "2.1.0",
        "tag": "v2.1.0",
        "commit": "a" * 40,
        "channel": "stable",
        "artifacts": [{
            "name": "revl-2.1.0.tar.gz",
            "kind": "sdist",
            "url": "https://example.invalid/revl-2.1.0.tar.gz",
            "sha256": "b" * 64,
            "size": 10,
        }],
        "live": False,
        "hostChanges": True,
        "pins": {"revl": "a" * 40, "cordis": None},
    }


def _keys(**overrides) -> tuple[dict, ...]:
    entry = {
        "keyId": IDENTITY.key_id,
        "publicKey": IDENTITY.public_key.hex(),
        "notBefore": NOT_BEFORE,
        "notAfter": NOT_AFTER,
    }
    entry.update(overrides)
    return (entry,)


def _signed() -> dict:
    return sign_record(RELEASE_DOMAIN, _document(), IDENTITY)


def test_a_signed_manifest_verifies_against_its_pinned_key():
    ok, reason = verify_manifest(_signed(), keys=_keys(), now=NOW)
    assert ok, reason
    assert IDENTITY.key_id in reason
    assert require_signed(_signed(), trust_unsigned=False, keys=_keys(),
                          now=NOW) == {"keyId": IDENTITY.key_id,
                                       "notAfter": NOT_AFTER}


def test_a_tampered_field_is_refused():
    document = _signed()
    document["version"] = "9.9.9"
    ok, reason = verify_manifest(document, keys=_keys(), now=NOW)
    assert not ok
    assert "signature" in reason
    assert "9.9.9" not in reason


def test_a_missing_signature_is_refused():
    document = _signed()
    del document["signature"]
    ok, reason = verify_manifest(document, keys=_keys(), now=NOW)
    assert not ok
    assert "signature" in reason


def test_an_unknown_key_id_is_refused():
    document = _signed()
    document["key_id"] = "0" * 16
    ok, reason = verify_manifest(document, keys=_keys(), now=NOW)
    assert not ok
    assert "not pinned" in reason


def test_an_expired_key_is_refused():
    ok, reason = verify_manifest(_signed(), keys=_keys(),
                                 now=datetime(2027, 1, 2,
                                              tzinfo=timezone.utc))
    assert not ok
    assert "expired" in reason


def test_a_not_yet_valid_key_is_refused():
    ok, reason = verify_manifest(_signed(), keys=_keys(),
                                 now=datetime(2025, 12, 31,
                                              tzinfo=timezone.utc))
    assert not ok
    assert "not yet valid" in reason
    assert "notBefore" in reason


def test_an_empty_pin_refuses_a_networked_manifest():
    ok, reason = verify_manifest(_signed(), keys=(), now=NOW)
    assert not ok
    assert "no release signing key is pinned" in reason


def test_a_malformed_pinned_entry_fails_closed():
    for broken in (
        _keys(notBefore="not-a-time"),
        _keys(publicKey="zz" * 64),
        _keys(keyId=IDENTITY.key_id, notAfter=""),
    ):
        ok, reason = verify_manifest(_signed(), keys=broken, now=NOW)
        assert not ok
        assert reason


def test_trust_unsigned_bypasses_verification():
    unsigned = _document()
    assert require_signed(unsigned, trust_unsigned=True, keys=_keys(),
                          now=NOW) is None


def test_an_unsigned_manifest_raises_update_unsigned():
    with pytest.raises(UpdateError) as raised:
        require_signed(_document(), trust_unsigned=False, keys=_keys(),
                       now=NOW)
    error = raised.value
    assert error.code == "UPDATE_UNSIGNED"
    assert "not signed" in error.message
    assert "--update-trust-unsigned" in (error.fix or "")


def test_keygen_writes_a_private_identity_that_signs_a_verifiable_manifest(
        tmp_path):
    private_key = tmp_path / "release-key.json"
    generated = subprocess.run(
        [sys.executable, str(TOOL), "keygen",
         "--private-key-out", str(private_key),
         "--peer-id", "revl-release",
         "--not-before", NOT_BEFORE,
         "--not-after", NOT_AFTER],
        cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)
    assert generated.returncode == 0, generated.stderr

    # The printed entry is the pin, and the private file is the secret.
    entry_line = next(line for line in generated.stdout.splitlines()
                      if line.startswith('{"keyId"'))
    entry = json.loads(entry_line)
    assert len(entry["keyId"]) == 16
    assert entry["keyId"] == entry["keyId"].lower()
    assert len(entry["publicKey"]) == 128
    assert entry["notBefore"] == NOT_BEFORE
    assert entry["notAfter"] == NOT_AFTER
    assert private_key.stat().st_mode & 0o777 == 0o600
    written = load_private_identity(private_key)
    assert written.key_id == entry["keyId"]
    assert written.public_key.hex() == entry["publicKey"]
    # The scalar must not appear on stdout, not even in the instructions.
    scalar_hex = f"{written.private_key:064x}"
    assert scalar_hex not in generated.stdout

    manifest = tmp_path / "release.json"
    manifest.write_text(json.dumps(_document()), encoding="utf-8")
    signed_path = tmp_path / "signed.json"
    signed = subprocess.run(
        [sys.executable, str(TOOL), "sign", str(manifest),
         "--key-file", str(private_key), "--out", str(signed_path)],
        cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)
    assert signed.returncode == 0, signed.stderr
    assert entry["keyId"] in signed.stdout
    document = json.loads(signed_path.read_text(encoding="utf-8"))
    ok, reason = verify_manifest(document, keys=(entry,), now=NOW)
    assert ok, reason


def test_keygen_refuses_a_key_inside_the_repository(tmp_path):
    target = ROOT / "release-key-that-must-not-be-written.json"
    refused = subprocess.run(
        [sys.executable, str(TOOL), "keygen",
         "--private-key-out", str(target)],
        cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)
    assert refused.returncode != 0
    assert "inside the repository" in refused.stderr
    assert not target.exists()

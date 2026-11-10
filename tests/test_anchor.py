"""Chain-neutral anchor records (issue #2723).

An anchor commits to a SET of existing sha256 digests (a composition hash, a
signed attestation, two peer-pool receipt digests) and signs the set under the
existing attestation key, so an operator can publish one value to an external
log or timestamping service. These tests pin the definition of done:

  * an anchor built from a composition hash and two receipt digests verifies;
  * changing one digest makes verification fail, and a forged anchor that was
    validly signed is still caught by the digests the verifier holds;
  * a record that claims an anchor_hash its digests do not hash to is refused;
  * an unknown version is refused even when its signature is authentic;
  * the anchor domain is separate from the attestation domain under one key;
  * the CLI emits an anchor, verifies it, and exits nonzero on a changed
    digest, a wrong key or an unknown version.
"""

import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import anchor as N  # noqa: E402
from revl import attest as A  # noqa: E402
from revl import compile_source  # noqa: E402
from revl.compiler import compile_files  # noqa: E402
from revl import pool_receipt  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.errors import RevlError  # noqa: E402

# a stable, admissible composition
BASE = """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission[db] fn put(key: Str, value: Str) }

component PgCache requires db: Database provides cache: Cache {
  provide cache { fn put(key, value) { emit db.execute(`INSERT ${key}`) } }
}
component Front requires cache: Cache { }
"""

KEY = b"anchor-test-signer-secret-0123456789"
OTHER_KEY = b"a-different-anchor-secret-9876"
# a fixed timestamp so `make_anchor` is a pure function under test
NOW = "2026-08-25T00:00:00+00:00"


def _composition_hash() -> str:
    return A.canonical_hash(compile_source(BASE, filename="<string>"))


def _receipt_digests() -> dict[str, str]:
    """Two digests taken from real receipt records, not arbitrary hex."""
    first = {"kind": pool_receipt.RECEIPT_KIND, "task_id": "task-one",
             "result_digest": A.canonical_hash({"result": 1})}
    second = {"kind": pool_receipt.RECEIPT_KIND, "task_id": "task-two",
              "result_digest": A.canonical_hash({"result": 2})}
    return {"receipt:one": pool_receipt.receipt_digest(first),
            "receipt:two": pool_receipt.receipt_digest(second)}


def _digests() -> dict[str, str]:
    return {"composition": _composition_hash(), **_receipt_digests()}


def _anchor(key: bytes = KEY, **kw) -> dict:
    return N.make_anchor(_digests(), key, now=NOW, **kw)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _key_file(tmp_path: Path, key: bytes = KEY,
              name: str = "anchor.key") -> Path:
    path = tmp_path / name
    path.write_bytes(key)
    return path


def _sign_again(document: dict, key: bytes = KEY) -> dict:
    """Re-sign a deliberately altered record, so the test exercises the check
    after the signature rather than the signature check itself."""
    document["signature"] = N._sign(document, key)
    return document


# ------------------------------------------------------------- building

def test_anchor_roundtrips_from_a_composition_hash_and_two_receipt_digests():
    digests = _digests()
    document = N.make_anchor(digests, KEY, now=NOW, signer="ci@revl")

    ok, reason = N.verify_anchor(document, KEY)

    assert ok, reason
    assert "valid" in reason
    assert document["kind"] == "revl.anchor"
    assert document["version"] == "1.0"
    assert document["hash_alg"] == "sha256"
    assert document["sign_alg"] == "hmac-sha256"
    assert document["timestamp"] == NOW
    assert document["signer"] == "ci@revl"
    assert document["key_id"] == A.key_id(KEY)
    assert document["digests"] == digests
    assert len(document["signature"]) == 64


def test_anchor_hash_is_the_documented_formula():
    """The published value must be recomputable by a verifier that does not
    import revl, so the exact canonical bytes are pinned here."""
    digests = _digests()
    expected = hashlib.sha256(json.dumps(
        digests, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")).hexdigest()

    assert N.make_anchor(digests, KEY, now=NOW)["anchor_hash"] == expected


def test_anchor_is_deterministic_given_now():
    first = N.make_anchor(_digests(), KEY, now=NOW, signer="ci")
    second = N.make_anchor(_digests(), KEY, now=NOW, signer="ci")
    assert first == second


# ------------------------------------------------------------- tampering

def test_one_changed_digest_fails_verification():
    document = _anchor()
    document["digests"]["receipt:two"] = "0" * 64

    ok, reason = N.verify_anchor(document, KEY)

    assert not ok
    assert "mismatch" in reason


def test_a_validly_signed_forgery_is_caught_by_the_digests_the_verifier_holds():
    """A key holder signs freely. When the verifier knows the composition and
    receipt digests it expects, the anchor must not be able to commit to
    different ones even under a real signature."""
    expected = _digests()
    forged = dict(expected)
    forged["receipt:one"] = "f" * 64
    document = _sign_again(N.make_anchor(forged, KEY, now=NOW))

    ok, reason = N.verify_anchor(document, KEY, expected=expected)

    assert not ok
    assert reason.startswith("digest mismatch")


def test_expected_digest_missing_from_the_anchor_is_refused():
    document = _anchor()
    expected = {**_digests(), "receipt:three": "c" * 64}

    ok, reason = N.verify_anchor(document, KEY, expected=expected)

    assert not ok
    assert "commits to no 'receipt:three' digest" in reason


def test_expected_digest_that_matches_is_accepted():
    digests = _digests()
    document = N.make_anchor(digests, KEY, now=NOW)

    ok, reason = N.verify_anchor(document, KEY,
                                 expected={"composition": digests["composition"]})

    assert ok, reason


def test_a_signed_anchor_hash_that_does_not_match_is_refused():
    """The signature proves the record was authored. The recomputation proves
    what it authored is a commitment to the digests it carries."""
    document = _anchor()
    document["anchor_hash"] = "e" * 64
    _sign_again(document)

    ok, reason = N.verify_anchor(document, KEY)

    assert not ok
    assert reason.startswith("anchor hash mismatch")


def test_wrong_key_fails_with_a_signature_mismatch():
    ok, reason = N.verify_anchor(_anchor(), OTHER_KEY)
    assert not ok
    assert reason.startswith("signature mismatch")


def test_unknown_version_is_refused_even_when_authentic():
    document = _anchor()
    document["version"] = "9.9"
    _sign_again(document)

    ok, reason = N.verify_anchor(document, KEY)

    assert not ok
    assert "unsupported anchor version" in reason


def test_a_mislabelled_algorithm_is_refused_after_an_authentic_signature():
    document = _anchor()
    document["sign_alg"] = "ed25519"
    _sign_again(document)

    ok, reason = N.verify_anchor(document, KEY)

    assert not ok
    assert "sign_alg" in reason


def test_a_signature_that_is_not_ascii_is_a_reason_and_not_a_raise():
    document = _anchor()
    document["signature"] = "\ud800"
    ok, reason = N.verify_anchor(document, KEY)
    assert not ok
    assert reason


def test_anchor_and_attestation_do_not_verify_as_each_other():
    """One key signs both protocols, so the MAC domains are what keep an
    anchor from being read as an attestation (and the reverse)."""
    verdict = A.run_gate(source=BASE, filename="<string>")
    attestation = A.make_attestation(verdict.ir, KEY, verdict=verdict, now=NOW)

    att_as_anchor, reason = N.verify_anchor(attestation, KEY)
    assert not att_as_anchor
    assert reason.startswith("signature mismatch")

    # `verify_attestation` refuses an anchor on its missing members before it
    # reaches the MAC, so add the one it requires to exercise the domain.
    lookalike = _anchor()
    lookalike["composition_hash"] = "a" * 64
    _sign_again(lookalike)
    anchor_as_att, reason = A.verify_attestation(lookalike, KEY)
    assert not anchor_as_att
    assert reason.startswith("signature mismatch")


def _sign_in_domain(document: dict, key: bytes, domain: bytes) -> str:
    signed = {member: value for member, value in document.items()
              if member != "signature"}
    return hmac.new(key, domain + A._canonical_bytes(signed),
                    hashlib.sha256).hexdigest()


@pytest.mark.parametrize("domain_name", [
    "attestation", "certificate", "deploy-receipt", "pool-receipt",
    "pool-receipt-attestation",
])
def test_a_mac_from_another_protocol_domain_does_not_verify_as_an_anchor(
        domain_name):
    """One key signs several protocols. Each records its own domain tag in the
    MAC, so a valid MAC for another protocol is not a valid anchor MAC."""
    from revl import cert, deploy  # noqa: PLC0415

    domains = {
        "attestation": A.SIGN_DOMAIN,
        "certificate": cert.CERT_SIGN_DOMAIN,
        "deploy-receipt": deploy.RECEIPT_DOMAIN,
        "pool-receipt": pool_receipt.RECEIPT_DOMAIN,
        "pool-receipt-attestation": pool_receipt.ATTESTATION_DOMAIN,
    }
    document = _anchor()
    document["signature"] = _sign_in_domain(document, KEY,
                                            domains[domain_name])

    ok, reason = N.verify_anchor(document, KEY)

    assert not ok
    assert reason.startswith("signature mismatch")


def test_a_record_that_names_another_key_fingerprint_is_refused():
    """key_id is what a reader uses to choose which key to fetch. A MAC that
    verifies under a key the record does not name has no answer to that."""
    document = _anchor()
    document["key_id"] = A.key_id(OTHER_KEY)
    _sign_again(document)

    ok, reason = N.verify_anchor(document, KEY)

    assert not ok
    assert reason.startswith("identity mismatch")


# ------------------------------------------------------------- shape refusals

@pytest.mark.parametrize("digests", [
    {},
    {"has space": "a" * 64},
    {"": "a" * 64},
    {"composition": "A" * 64},
    {"composition": "a" * 63},
    {"composition": "z" * 64},
    {"composition": 7},
])
def test_an_unaddressable_digest_set_is_refused(digests):
    with pytest.raises(RevlError):
        N.make_anchor(digests, KEY, now=NOW)


def test_an_empty_key_is_refused():
    with pytest.raises(RevlError):
        N.make_anchor(_digests(), b"", now=NOW)


def test_load_anchor_refuses_a_document_that_is_not_an_object(tmp_path):
    path = _write(tmp_path / "anchor.json", "[1, 2, 3]")
    with pytest.raises(RevlError):
        N.load_anchor(str(path))


def test_parse_digest_arg_accepts_the_same_spelling_at_both_ends():
    assert N.parse_digest_arg("receipt:one=" + "a" * 64) == (
        "receipt:one", "a" * 64)
    for bad in ("no-equals", "name=" + "A" * 64, "bad name=" + "a" * 64):
        with pytest.raises(RevlError):
            N.parse_digest_arg(bad)


# ------------------------------------------------------------- CLI

def test_cli_emit_and_verify_roundtrip(tmp_path, capsys):
    comp = _write(tmp_path / "base.rvl", BASE)
    keyf = _key_file(tmp_path)
    d1, d2 = _receipt_digests().values()

    rc = main(["attest", "--anchor", str(comp),
               "--digest", f"receipt:one={d1}",
               "--digest", f"receipt:two={d2}",
               "--key", str(keyf), "--json"])
    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    assert document["kind"] == "revl.anchor"
    assert document["digests"]["composition"] == A.canonical_hash(
        compile_files([str(comp)]))

    anchor_path = _write(tmp_path / "anchor.json", json.dumps(document))
    rc = main(["verify", "--anchor", str(anchor_path), "--key", str(keyf)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "VALID" in out
    assert document["anchor_hash"] in out


def test_cli_emit_without_a_composition_or_digest_is_refused(tmp_path, capsys):
    keyf = _key_file(tmp_path)
    rc = main(["attest", "--anchor", "--key", str(keyf)])
    assert rc == 1
    assert "--digest" in capsys.readouterr().err


def test_cli_emit_refuses_duplicate_digest_names(tmp_path, capsys):
    keyf = _key_file(tmp_path)
    d = "a" * 64
    rc = main(["attest", "--anchor", "--digest", f"receipt:one={d}",
               "--digest", f"receipt:one={d}", "--key", str(keyf)])
    assert rc == 1
    assert "more than once" in capsys.readouterr().err


def test_cli_verify_detects_a_changed_digest_nonzero(tmp_path, capsys):
    keyf = _key_file(tmp_path)
    document = _anchor()
    document["digests"]["receipt:two"] = "0" * 64
    anchor_path = _write(tmp_path / "anchor.json", json.dumps(document))

    rc = main(["verify", "--anchor", str(anchor_path), "--key", str(keyf)])

    assert rc == 1
    assert "INVALID" in capsys.readouterr().out


def test_cli_verify_unknown_version_nonzero(tmp_path, capsys):
    keyf = _key_file(tmp_path)
    document = _sign_again(_anchor())
    document["version"] = "9.9"
    _sign_again(document)
    anchor_path = _write(tmp_path / "anchor.json", json.dumps(document))

    rc = main(["verify", "--anchor", str(anchor_path), "--key", str(keyf)])

    assert rc == 1
    out = capsys.readouterr().out
    assert "INVALID" in out
    assert "unsupported anchor version" in out


def test_cli_verify_compares_the_digests_the_verifier_holds(tmp_path, capsys):
    keyf = _key_file(tmp_path)
    document = _anchor()
    anchor_path = _write(tmp_path / "anchor.json", json.dumps(document))
    d = document["digests"]["composition"]

    rc = main(["verify", "--anchor", str(anchor_path), "--key", str(keyf),
               "--digest", f"composition={d}"])
    assert rc == 0
    assert "VALID" in capsys.readouterr().out

    rc = main(["verify", "--anchor", str(anchor_path), "--key", str(keyf),
               "--digest", f"composition={'0' * 64}"])
    assert rc == 1
    assert "digest mismatch" in capsys.readouterr().out


def test_cli_verify_wrong_key_nonzero(tmp_path, capsys):
    keyf = _key_file(tmp_path)
    wrongf = _key_file(tmp_path, OTHER_KEY, "wrong.key")
    anchor_path = _write(tmp_path / "anchor.json", json.dumps(_anchor()))

    rc = main(["verify", "--anchor", str(anchor_path), "--key", str(wrongf)])

    assert rc == 1
    assert "signature mismatch" in capsys.readouterr().out
    assert keyf.exists()


def test_cli_verify_missing_key_is_a_read_error(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv(A.KEY_ENV, raising=False)
    monkeypatch.delenv(A.KEY_FILE_ENV, raising=False)
    anchor_path = _write(tmp_path / "anchor.json", json.dumps(_anchor()))

    rc = main(["verify", "--anchor", str(anchor_path)])

    assert rc == 2
    assert "no signing key" in capsys.readouterr().err


def test_cli_verify_unreadable_anchor_is_a_read_error(tmp_path, capsys):
    keyf = _key_file(tmp_path)
    rc = main(["verify", "--anchor", str(tmp_path / "missing.json"),
               "--key", str(keyf)])
    assert rc == 2
    assert "cannot verify" in capsys.readouterr().err


def test_cli_verify_needs_a_bundle_or_an_anchor(capsys):
    rc = main(["verify"])
    assert rc == 2
    assert "needs a bundle" in capsys.readouterr().err


def test_cli_verify_refuses_a_bundle_and_an_anchor_together(tmp_path, capsys):
    keyf = _key_file(tmp_path)
    anchor_path = _write(tmp_path / "anchor.json", json.dumps(_anchor()))
    rc = main(["verify", str(tmp_path / "bundle"), "--anchor",
               str(anchor_path), "--key", str(keyf)])
    assert rc == 2
    assert "not both" in capsys.readouterr().err


def test_cli_attest_without_a_target_is_a_usage_error(capsys):
    rc = main(["attest"])
    assert rc == 2
    assert "needs a composition TARGET" in capsys.readouterr().err


@pytest.mark.parametrize("extra", [
    ["--key", "k"],
    ["--digest", "composition=" + "a" * 64],
])
def test_cli_verify_refuses_anchor_options_without_an_anchor(tmp_path, capsys,
                                                             extra):
    rc = main(["verify", str(tmp_path / "bundle"), *extra])
    assert rc == 2
    assert "apply only with --anchor" in capsys.readouterr().err


def test_cli_verify_without_anchor_still_routes_to_the_bundle_verifier(
        tmp_path, capsys):
    rc = main(["verify", str(tmp_path / "missing.revlbundle")])
    assert rc == 2
    assert "cannot verify" in capsys.readouterr().err


def test_cli_attest_verify_without_a_target_is_a_usage_error(capsys):
    rc = main(["attest", "--verify"])
    assert rc == 2
    assert "needs a record to check" in capsys.readouterr().err


def test_cli_attest_anchor_refuses_verify_mode(tmp_path, capsys):
    keyf = _key_file(tmp_path)
    rc = main(["attest", "--anchor", "--verify",
               "--digest", "receipt:one=" + "a" * 64, "--key", str(keyf)])
    assert rc == 1
    assert "cannot be combined" in capsys.readouterr().err


def test_cli_attest_refuses_digest_without_anchor(tmp_path, capsys,
                                                 monkeypatch):
    """A normal attestation must not silently drop a digest the operator asked
    to commit to. The refusal happens before key resolution, so a missing key
    cannot mask it."""
    comp = _write(tmp_path / "base.rvl", BASE)
    monkeypatch.delenv(A.KEY_ENV, raising=False)
    monkeypatch.delenv(A.KEY_FILE_ENV, raising=False)
    rc = main(["attest", str(comp), "--digest", "composition=" + "a" * 64])
    err = capsys.readouterr().err
    assert rc == 2
    assert "--digest applies only with --anchor" in err
    assert "no signing key" not in err

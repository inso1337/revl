"""`revl attest` — cryptographic attestation of verified compositions
(roadmap item 127).

After the gate admits a composition, `revl attest` signs a portable record that
*this exact* composition passed: a canonical hash of its admitted IR, the
verdict, the guarantees that verdict proves, a timestamp, and a signer
identity, protected by an HMAC-SHA256 signature keyed by a signer secret.
`revl attest --verify` checks such a record.

These tests pin the definition of done:

  * attest -> verify round-trips **valid** with the right key;
  * a **tampered composition** fails verify with a *hash mismatch* (the
    composition changed) — distinct from
  * a **wrong key** (or an edited attestation) failing with a *signature
    mismatch*;
  * the IR content hash is **deterministic** for identical IR and changes when
    the composition changes;
  * the `--json` attestation has the documented shape;
  * the CLI works both directions, including a **nonzero exit** on an invalid
    `--verify` (it is a check, not a report);
  * a **draft** (open holes) is refused — it compiled but was never admitted.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl import attest as A  # noqa: E402


# a stable, admissible two-component composition
BASE = """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission[db] fn put(key: Str, value: Str) }

component PgCache requires db: Database provides cache: Cache {
  provide cache { fn put(key, value) { emit db.execute(`INSERT ${key}`) } }
}
component Front requires cache: Cache { }
"""

# a *different* admissible composition — Front gains an emission it did not have
CHANGED = """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission[db] fn put(key: Str, value: Str) }

component PgCache requires db: Database provides cache: Cache {
  provide cache { fn put(key, value) { emit db.execute(`INSERT ${key}`) } }
}
component Front requires cache: Cache { emit cache.put("k", "v") }
"""

KEY = b"test-signer-secret-0123456789"
OTHER_KEY = b"a-different-secret-value-9876"
# a fixed timestamp so `make_attestation` is a pure function under test
NOW = "2026-08-25T00:00:00+00:00"


def _att(src: str = BASE, key: bytes = KEY, **kw) -> dict:
    return A.make_attestation(compile_source(src), key, now=NOW, **kw)


# ------------------------------------------------------------- canonical hash

def test_hash_is_deterministic_for_identical_ir():
    a = A.canonical_hash(compile_source(BASE))
    b = A.canonical_hash(compile_source(BASE))
    assert a == b
    assert len(a) == 64  # sha256 hex


def test_hash_changes_when_composition_changes():
    assert A.canonical_hash(compile_source(BASE)) \
        != A.canonical_hash(compile_source(CHANGED))


# --------------------------------------------------------------- round-trip

def test_attest_then_verify_is_valid():
    att = _att()
    ir = compile_source(BASE)
    ok, reason = A.verify_attestation(att, KEY, ir)
    assert ok is True
    assert "valid" in reason.lower()


def test_verify_without_ir_checks_signature_only():
    att = _att()
    ok, reason = A.verify_attestation(att, KEY)  # no composition supplied
    assert ok is True


def test_attestation_is_deterministic_given_now():
    assert _att() == _att()  # pure function of (ir, key, now, signer)


# ------------------------------------------------------ failure mode: hash

def test_tampered_composition_fails_with_hash_mismatch():
    att = _att(BASE)                       # attested the BASE composition
    ir = compile_source(CHANGED)           # verifying a different one
    ok, reason = A.verify_attestation(att, KEY, ir)
    assert ok is False
    assert "hash mismatch" in reason
    assert "changed" in reason


# ------------------------------------------------------ failure mode: sig

def test_wrong_key_fails_with_signature_mismatch():
    att = _att(BASE, key=KEY)
    ok, reason = A.verify_attestation(att, OTHER_KEY, compile_source(BASE))
    assert ok is False
    assert "signature mismatch" in reason


def test_edited_attestation_fails_with_signature_mismatch():
    att = _att(BASE)
    att["verdict"] = "rejected"            # tamper with a signed member
    ok, reason = A.verify_attestation(att, KEY, compile_source(BASE))
    assert ok is False
    assert "signature mismatch" in reason


def test_forged_hash_in_attestation_fails_signature():
    # a tamperer swaps in the hash of a different composition to make --against
    # pass; the signature no longer matches the edited payload.
    att = _att(BASE)
    att["composition_hash"] = A.canonical_hash(compile_source(CHANGED))
    ok, reason = A.verify_attestation(att, KEY, compile_source(CHANGED))
    assert ok is False
    assert "signature mismatch" in reason


# ------------------------------------------------------------- attested body

def test_attestation_shape_and_guarantees():
    att = _att(BASE, signer="ci@revl")
    assert att["kind"] == "revl.attestation"
    assert att["verdict"] == "admitted"
    assert att["hash_alg"] == "sha256"
    assert att["sign_alg"] == "hmac-sha256"
    assert att["composition_hash"] == A.canonical_hash(compile_source(BASE))
    assert att["timestamp"] == NOW
    assert att["signer"] == "ci@revl"
    assert len(att["signature"]) == 64
    # the guarantees an admitted verdict proves: the composition G-rules
    assert att["guarantees"] == ["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8"]
    # key_id is a non-secret fingerprint — never the key itself
    assert att["key_id"] == A.key_id(KEY)
    assert KEY.decode() not in json.dumps(att)


def test_draft_with_open_holes_is_not_attestable():
    draft = (
        "service Cache { fn get(key: Str) -> Str }\n"
        "component C provides c: Cache {\n"
        '  provide c { fn get(key) = hole "look up in the store" }\n'
        "}\n"
    )
    ir = compile_source(draft)
    assert ir.get("holes")  # it compiled, but it is a draft
    with pytest.raises(Exception) as exc:
        A.make_attestation(ir, KEY, now=NOW)
    assert "hole" in str(exc.value)


def test_empty_key_is_rejected():
    with pytest.raises(Exception):
        A.make_attestation(compile_source(BASE), b"", now=NOW)
    ok, reason = A.verify_attestation(_att(), b"")
    assert ok is False


# ---------------------------------------------------------------- key IO

def test_load_key_strips_trailing_newline(tmp_path):
    p = tmp_path / "signer.key"
    p.write_bytes(KEY + b"\n")
    assert A.load_key(str(p)) == KEY


def test_resolve_key_prefers_path_then_file_env_then_inline(tmp_path):
    p = tmp_path / "k.key"
    p.write_bytes(KEY)
    assert A.resolve_key(str(p), env={}) == KEY
    assert A.resolve_key(None, env={A.KEY_FILE_ENV: str(p)}) == KEY
    assert A.resolve_key(None, env={A.KEY_ENV: "inline-secret"}) == b"inline-secret"
    with pytest.raises(Exception):
        A.resolve_key(None, env={})


# ------------------------------------------------------------------- CLI

def _key_file(tmp_path) -> str:
    p = tmp_path / "signer.key"
    p.write_bytes(KEY)
    return str(p)


def _write(tmp_path, name: str, src: str) -> str:
    p = tmp_path / name
    p.write_text(src)
    return str(p)


def test_cli_sign_and_verify_roundtrip(tmp_path, capsys):
    comp = _write(tmp_path, "base.rvl", BASE)
    keyf = _key_file(tmp_path)

    rc = main(["attest", comp, "--key", keyf, "--json"])
    assert rc == 0
    att = json.loads(capsys.readouterr().out)
    assert att["verdict"] == "admitted"

    att_path = tmp_path / "att.json"
    att_path.write_text(json.dumps(att))

    # verify signature + composition still matches -> valid, exit 0
    rc = main(["attest", str(att_path), "--verify", "--against", comp,
               "--key", keyf])
    out = capsys.readouterr().out
    assert rc == 0
    assert "VALID" in out


def test_cli_verify_detects_changed_composition_nonzero(tmp_path, capsys):
    comp = _write(tmp_path, "base.rvl", BASE)
    changed = _write(tmp_path, "changed.rvl", CHANGED)
    keyf = _key_file(tmp_path)

    main(["attest", comp, "--key", keyf, "--json"])
    att = json.loads(capsys.readouterr().out)
    att_path = tmp_path / "att.json"
    att_path.write_text(json.dumps(att))

    rc = main(["attest", str(att_path), "--verify", "--against", changed,
               "--key", keyf])
    out = capsys.readouterr().out
    assert rc == 1                      # a check fails nonzero
    assert "INVALID" in out
    assert "hash mismatch" in out


def test_cli_verify_wrong_key_nonzero(tmp_path, capsys):
    comp = _write(tmp_path, "base.rvl", BASE)
    keyf = _key_file(tmp_path)
    main(["attest", comp, "--key", keyf, "--json"])
    att = json.loads(capsys.readouterr().out)
    att_path = tmp_path / "att.json"
    att_path.write_text(json.dumps(att))

    wrongf = tmp_path / "wrong.key"
    wrongf.write_bytes(OTHER_KEY)
    rc = main(["attest", str(att_path), "--verify", "--key", str(wrongf)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "signature mismatch" in out


def test_cli_key_from_env(tmp_path, capsys, monkeypatch):
    comp = _write(tmp_path, "base.rvl", BASE)
    monkeypatch.setenv(A.KEY_ENV, "env-secret-value")
    rc = main(["attest", comp, "--json"])
    assert rc == 0
    att = json.loads(capsys.readouterr().out)

    att_path = tmp_path / "att.json"
    att_path.write_text(json.dumps(att))
    rc = main(["attest", str(att_path), "--verify", "--against", comp])
    assert rc == 0


def test_cli_missing_key_errors(tmp_path, capsys, monkeypatch):
    comp = _write(tmp_path, "base.rvl", BASE)
    monkeypatch.delenv(A.KEY_ENV, raising=False)
    monkeypatch.delenv(A.KEY_FILE_ENV, raising=False)
    rc = main(["attest", comp])
    assert rc == 1
    assert "no signing key" in capsys.readouterr().err

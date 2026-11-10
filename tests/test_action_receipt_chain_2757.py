"""Signed, chained receipts for consequential actions (issue #2757).

The item's acceptance is three claims, and each has its own group here:

  * a three-receipt chain exports and verifies, and deleting the middle, swapping
    two or altering one field is DETECTED (the issue's exit clause). The
    alteration test walks EVERY member of the body, so a member added later is
    covered without anyone remembering to extend the list;
  * a receipt for a call that handled a secret carries no value digest and
    names why, and the disclosure rule holds in both directions: a key holder
    cannot mint a salted digest over a secret origin, and cannot suppress an
    ordinary value with no disclosure origin;
  * the chain is signed and fail-closed: an unknown key is a refusal, the
    session verdict and key fingerprint are inside the MAC, and a key rotation
    across a chain verifies against the union of the keys it names.

The CLI half (`revl receipts export` / `verify`) is pinned at the end, because a
receipt nobody can issue or check is a library fact rather than a reachable one.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import action_receipt as R  # noqa: E402
from revl import attest  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.errors import RevlError  # noqa: E402

KEY = b"an-action-receipt-test-key"
KEY2 = b"a-rotated-action-receipt-test-key"
SALT = "ab" * 32
NOW = "2026-01-01T00:00:00+00:00"
KEY_ENVS = (R.KEY_ENV, R.KEY_FILE_ENV)


def _action(**overrides) -> dict:
    action = {
        "receipt_kind": "effect-crossing",
        "task": "task-1",
        "component": "ledger",
        "bundle": "1" * 64,
        "policy": "2" * 64,
        "capability": "db.insert",
        "approval": None,
        "worker": "worker-1",
        "input": {"row": {"amount": 10}},
        "input_origins": ["input"],
        "result": {"inserted": 1},
        "result_origins": [],
    }
    action.update(overrides)
    return action


def _actions() -> list[dict]:
    return [
        _action(task="task-1", capability="db.insert"),
        _action(task="task-2", capability="db.insert",
                approval={"request_id": "req-1", "use": "confirm"}),
        _action(receipt_kind="approval-use", task="task-2",
                capability="db.insert",
                approval={"request_id": "req-1", "use": "confirm"}),
    ]


def _chain(actions=None, key=KEY, verdict="committed", salt=SALT) -> dict:
    return R.export_chain(key, session="session-1",
                          actions=_actions() if actions is None else actions,
                          session_verdict=verdict, salt=salt, now=NOW)


def _resign(receipt: dict, key: bytes = KEY) -> dict:
    """Recompute a receipt's digest and MAC, so a test can present a
    well-formed body that a signer holding the key could actually mint."""
    receipt.pop(R.SIGNATURE_FIELD, None)
    receipt["digest"] = R.receipt_digest(receipt)
    receipt[R.SIGNATURE_FIELD] = R._sign(receipt, key, R.SIGN_DOMAIN)
    return receipt


def _resign_chain(chain: dict, key: bytes = KEY) -> dict:
    chain[R.SIGNATURE_FIELD] = R._sign(chain, key, R.CHAIN_SIGN_DOMAIN,
                                       members=R.CHAIN_MEMBERS)
    return chain


# ---------------------------------------------------------------------------
# the three-receipt chain and its tamper detection
# ---------------------------------------------------------------------------

def test_a_three_receipt_chain_verifies():
    chain = _chain()
    assert [receipt["seq"] for receipt in chain["receipts"]] == [0, 1, 2]
    assert chain["count"] == 3
    assert chain["head"] == chain["receipts"][-1]["digest"]
    assert chain["receipts"][0]["prev"] is None
    assert chain["receipts"][1]["prev"] == chain["receipts"][0]["digest"]
    assert chain["receipts"][2]["prev"] == chain["receipts"][1]["digest"]
    verdict = R.verify_chain(chain, KEY)
    assert verdict.ok, verdict.reason


def test_deleting_the_middle_receipt_is_detected():
    chain = _chain()
    del chain["receipts"][1]
    verdict = R.verify_chain(chain, KEY)
    assert not verdict.ok
    assert verdict.link in {"chain-order", "chain-link", "chain-head",
                            "chain-signature"}
    assert "receipt" in verdict.reason or "digest" in verdict.reason


def test_swapping_two_receipts_is_detected():
    chain = _chain()
    chain["receipts"][0], chain["receipts"][1] = (
        chain["receipts"][1], chain["receipts"][0])
    verdict = R.verify_chain(chain, KEY)
    assert not verdict.ok
    assert verdict.link in {"chain-order", "chain-link", "chain-signature"}


def test_truncating_the_tail_is_detected():
    chain = _chain()
    chain["receipts"].pop()
    verdict = R.verify_chain(chain, KEY)
    assert not verdict.ok
    assert verdict.link in {"chain-order", "chain-head", "chain-signature"}


def test_a_consistent_truncation_fails_the_chain_signature():
    """Tail truncation with count, digests and head edited to agree, and no
    re-sign: every body-level count agrees, so only the envelope MAC can
    refuse it. It does, and the refusal names that check."""
    chain = _chain()
    chain["receipts"].pop()
    chain["count"] = len(chain["receipts"])
    chain["digests"] = chain["digests"][:-1]
    chain["head"] = chain["digests"][-1]
    verdict = R.verify_chain(chain, KEY)
    assert not verdict.ok
    assert verdict.link == "chain-signature"


def test_every_receipt_member_is_covered():
    """The issue's "alters one field" clause, walked over the whole body: a
    member added to the record later is covered without editing this test."""
    chain = _chain()
    original = copy.deepcopy(chain["receipts"][1])
    mutations = {
        "kind": "revl.other",
        "version": "9.9",
        "hash_alg": "sha512",
        "sign_alg": "ed25519",
        "key_id": "0" * 16,
        "session": "other-session",
        "seq": 99,
        "prev": "0" * 64,
        "receipt_kind": "approval-use",
        "task": "tampered-task",
        "component": "elsewhere",
        "bundle": "0" * 64,
        "policy": None,
        "capability": "net.post",
        "approval": {"request_id": "forged", "use": None},
        "worker": "someone-else",
        "session_verdict": "aborted",
        "input": {"mode": "suppressed", "value": None,
                  "reason": "secret-origin"},
        "input_origins": ["secret"],
        "result": {"mode": "suppressed", "value": None,
                   "reason": "confidential-origin"},
        "result_origins": ["confidential"],
        "issued_at": "2030-01-01T00:00:00+00:00",
        "digest": "0" * 64,
    }
    assert set(mutations) == set(R.RECEIPT_BODY_MEMBERS)
    for member, value in mutations.items():
        chain["receipts"][1] = copy.deepcopy(original)
        chain["receipts"][1][member] = value
        verdict = R.verify_chain(chain, KEY)
        assert not verdict.ok, f"altering {member} was not detected"
    chain["receipts"][1] = original
    assert R.verify_chain(chain, KEY).ok


def test_chain_envelope_metadata_is_covered():
    for member, value in {
        "session": "other-session",
        "session_verdict": "aborted",
        "salt": "cd" * 32,
        "count": 2,
        "head": "0" * 64,
        "digests": ["0" * 64] * 3,
        "key_id": "0" * 16,
    }.items():
        chain = _chain()
        chain[member] = value
        verdict = R.verify_chain(chain, KEY)
        assert not verdict.ok, f"altering chain {member} was not detected"


def test_an_empty_chain_verifies_and_has_no_head():
    chain = _chain(actions=[])
    assert chain["count"] == 0
    assert chain["head"] is None
    assert R.verify_chain(chain, KEY).ok


# ---------------------------------------------------------------------------
# commitments and the disclosure rule
# ---------------------------------------------------------------------------

def test_a_secret_receipt_carries_no_value_digest_and_says_why():
    chain = _chain(actions=[_action(
        task="secret-task", input={"password": "hunter2"},
        input_origins=["secret"], result={"ok": True}, result_origins=[])])
    receipt = chain["receipts"][0]
    assert receipt["input"] == {"mode": "suppressed", "value": None,
                                "reason": "secret-origin"}
    assert receipt["input_origins"] == ["secret"]
    # the result itself is not secret, so its commitment is a digest
    assert receipt["result"]["mode"] == "salted"
    assert isinstance(receipt["result"]["value"], str)
    assert R.verify_chain(chain, KEY).ok


def test_a_confidential_receipt_carries_no_value_digest():
    chain = _chain(actions=[_action(
        input={"account": "12345"}, input_origins=["confidential"],
        result={"balance": 10}, result_origins=["confidential", "input"])])
    receipt = chain["receipts"][0]
    assert receipt["input"]["reason"] == "confidential-origin"
    assert receipt["result"]["reason"] == "confidential-origin"
    assert receipt["result"]["value"] is None
    assert R.verify_chain(chain, KEY).ok


def test_secret_wins_over_confidential_when_both_are_present():
    commitment = R.commitment("x", ["confidential", "secret"],
                              salt=SALT, label="input")
    assert commitment["mode"] == "suppressed"
    assert commitment["reason"] == "secret-origin"


def test_a_key_holder_cannot_mint_a_salted_digest_over_a_secret():
    chain = _chain(actions=[_action(input_origins=["secret"])])
    receipt = chain["receipts"][0]
    receipt["input"] = {"mode": "salted", "value": "0" * 64, "reason": None}
    _resign(receipt)
    verdict = R.verify_receipt(receipt, KEY)
    assert not verdict.ok
    assert verdict.link == "commitment"


def test_a_suppressed_commitment_needs_a_disclosure_origin():
    chain = _chain()
    receipt = chain["receipts"][0]
    receipt["input"] = {"mode": "suppressed", "value": None,
                        "reason": "secret-origin"}
    _resign(receipt)
    verdict = R.verify_receipt(receipt, KEY)
    assert not verdict.ok
    assert verdict.link == "commitment"


def test_a_commitment_is_salted_and_label_bound():
    same_input = R.commitment({"x": 1}, ["input"], salt=SALT, label="input")
    same_result = R.commitment({"x": 1}, ["input"], salt=SALT, label="result")
    other_salt = R.commitment({"x": 1}, ["input"], salt="cd" * 32,
                              label="input")
    assert same_input["value"] != same_result["value"]
    assert same_input["value"] != other_salt["value"]
    assert same_input == R.commitment({"x": 1}, ["input"], salt=SALT,
                                      label="input")


def test_unknown_origins_are_refused():
    with pytest.raises(ValueError, match="unknown origin"):
        R.commitment("x", ["not-an-origin"], salt=SALT, label="input")


# ---------------------------------------------------------------------------
# keys, rotation and the fail-closed surface
# ---------------------------------------------------------------------------

def test_a_chain_that_spans_a_rotation_verifies_against_both_keys():
    actions = _actions()
    actions[2]["key"] = KEY2
    chain = _chain(actions=actions)
    assert chain["receipts"][2]["key_id"] == R.key_id(KEY2)
    assert R.verify_chain(chain, [KEY, KEY2]).ok
    assert R.verify_chain(chain, {R.key_id(KEY): KEY,
                                  R.key_id(KEY2): KEY2}).ok


def test_a_missing_rotation_key_is_named_not_guessed():
    actions = _actions()
    actions[2]["key"] = KEY2
    chain = _chain(actions=actions)
    verdict = R.verify_chain(chain, KEY)
    assert not verdict.ok
    assert verdict.link == "key-unknown"
    assert "receipt 2" in verdict.reason


def test_another_key_is_refused():
    chain = _chain()
    verdict = R.verify_chain(chain, b"a-different-key")
    assert not verdict.ok
    assert verdict.link == "key-unknown"


def test_cross_protocol_domain_is_refused():
    """A receipt MACed under the attestation domain is not a receipt, even
    with the right key: the two protocols carry distinct tags."""
    chain = _chain()
    receipt = chain["receipts"][0]
    receipt[R.SIGNATURE_FIELD] = R._sign(receipt, KEY, attest.SIGN_DOMAIN)
    verdict = R.verify_receipt(receipt, KEY)
    assert not verdict.ok
    assert verdict.link == "signature"


def test_an_approval_use_receipt_needs_an_approval():
    with pytest.raises(RevlError, match="approval"):
        R.make_receipt(KEY, session="s", seq=0, prev=None,
                       receipt_kind="approval-use", task="t", component="c",
                       bundle=None, policy=None, capability="db.insert",
                       approval=None, worker="w",
                       session_verdict="committed", salt=SALT,
                       input="x", result="y")


def test_an_omitted_action_member_is_refused_at_export():
    action = _action()
    del action["capability"]
    with pytest.raises(RevlError, match="capability"):
        _chain(actions=[action])


def test_export_refuses_an_empty_chain_key():
    with pytest.raises(ValueError, match="non-empty bytes"):
        R.export_chain(b"", session="s", actions=[], salt=SALT, now=NOW)


@pytest.mark.parametrize("key", ["a-str-key", 12345, None, ["k"]])
def test_export_refuses_a_non_bytes_chain_key(key):
    with pytest.raises(ValueError, match="non-empty bytes"):
        R.export_chain(key, session="s", actions=[], salt=SALT, now=NOW)


def test_the_keyring_refuses_an_empty_key():
    for keys in (b"", [b""], [KEY, b""], {"0" * 16: b""}):
        with pytest.raises(ValueError, match="non-empty bytes"):
            R._keyring(keys)


def test_a_chain_signed_with_an_empty_key_is_not_verified(monkeypatch):
    """A MAC under an empty key is one anyone can compute. Build such a chain
    by lifting the signer's own refusal, then check no key shape accepts it."""
    monkeypatch.setattr(R, "_require_key", lambda key, what="": bytes(key),
                        raising=False)
    chain = R.export_chain(b"", session="s", actions=[], salt=SALT, now=NOW)
    monkeypatch.undo()
    for keys in (b"", [b""], {R.key_id(b""): b""}):
        verdict = R.verify_chain(chain, keys)
        assert not verdict.ok, f"verified against {keys!r}"
        assert verdict.link == "key-unknown"


def test_a_keyring_id_must_be_the_fingerprint_of_its_key():
    with pytest.raises(ValueError, match="fingerprint"):
        R._keyring({"0" * 16: KEY})
    assert R._keyring({R.key_id(KEY): KEY}) == {R.key_id(KEY): KEY}


def test_an_approval_with_extra_members_is_refused_at_export():
    action = _action(approval={"request_id": "req-1", "use": "confirm",
                               "note": "unbound"})
    with pytest.raises(RevlError, match="note"):
        _chain(actions=[action])


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch):
    for name in KEY_ENVS:
        monkeypatch.delenv(name, raising=False)


def _write_key(tmp_path: Path, name: str, key: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(key)
    return path


def _write_actions(tmp_path: Path, actions=None, verdict="committed") -> Path:
    path = tmp_path / "actions.json"
    path.write_text(json.dumps({"actions": _actions() if actions is None
                                else actions,
                                "session_verdict": verdict}))
    return path


def test_cli_exports_and_verifies(tmp_path, capsys):
    key = _write_key(tmp_path, "key", KEY)
    actions = _write_actions(tmp_path)
    chain = tmp_path / "chain.json"
    assert main(["receipts", "export", "--actions", str(actions),
                 "--session", "session-1", "--key", str(key),
                 "--out", str(chain)]) == 0
    document = json.loads(chain.read_text(encoding="utf-8"))
    assert document["kind"] == R.CHAIN_KIND
    assert main(["receipts", "verify", str(chain), "--key", str(key),
                 "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True


def test_cli_verify_refuses_a_tampered_chain(tmp_path):
    key = _write_key(tmp_path, "key", KEY)
    chain = tmp_path / "chain.json"
    _chain_document = _chain()
    _chain_document["receipts"][1]["task"] = "tampered"
    chain.write_text(json.dumps(_chain_document))
    assert main(["receipts", "verify", str(chain),
                 "--key", str(key)]) == 1


def test_cli_verify_without_a_key_is_a_refusal(tmp_path, capsys):
    chain = tmp_path / "chain.json"
    chain.write_text(json.dumps(_chain()))
    assert main(["receipts", "verify", str(chain)]) == 1
    assert "no key to verify against" in capsys.readouterr().out


def test_cli_export_never_prints_an_unsigned_chain(tmp_path, capsys):
    actions = _write_actions(tmp_path)
    assert main(["receipts", "export", "--actions", str(actions),
                 "--session", "session-1"]) == 1
    assert capsys.readouterr().out == ""


def test_cli_export_reads_the_key_from_the_environment(tmp_path, monkeypatch):
    key = _write_key(tmp_path, "key", KEY)
    monkeypatch.setenv(R.KEY_FILE_ENV, str(key))
    actions = _write_actions(tmp_path)
    chain = tmp_path / "chain.json"
    assert main(["receipts", "export", "--actions", str(actions),
                 "--session", "session-1", "--out", str(chain)]) == 0
    assert main(["receipts", "verify", str(chain)]) == 0


def test_verify_never_crashes_on_a_hostile_document():
    """A presented chain is untrusted input. A shape this build cannot read is
    a refusal, never an exception that escapes the verifier."""
    hostile = [
        [],
        "not a chain",
        {},
        {"kind": R.CHAIN_KIND},
        {"kind": R.CHAIN_KIND, "version": R.CHAIN_VERSION, "receipts": [1]},
        {"kind": R.CHAIN_KIND, "version": R.CHAIN_VERSION,
         "receipts": [{"input": {"mode": "salted"}, "input_origins": [["x"]]}]},
        {1: "non-string key"},
        _chain() | {"key_id": []},
        _chain() | {"receipts": [{"key_id": []}]},
    ]
    for document in hostile:
        verdict = R.verify_chain(document, KEY)
        assert not verdict.ok


def test_hostile_origins_and_salts_are_refused_without_crashing():
    with pytest.raises(ValueError):
        R.commitment("x", [["not", "a", "string"]], salt=SALT, label="input")
    with pytest.raises(ValueError):
        R.commitment("x", ["input"], salt="not-hex", label="input")


def test_a_huge_integer_is_refused_not_raised():
    """A JSON integer above Python's string-conversion limit has no printable
    form, so building a refusal message must not escape the verifier."""
    huge = 10 ** 5000
    assert not R.verify_chain({"kind": huge}, KEY).ok
    assert not R.verify_receipt({"signature": "0" * 64, "key_id": huge},
                                KEY).ok
    assert isinstance(R.check_body({"kind": huge}), str)


def test_cli_huge_integer_documents_are_refusals(tmp_path):
    """The CLI reads an untrusted document; a huge integer is a refusal with
    exit 1, not a traceback."""
    chain = tmp_path / "huge-chain.json"
    chain.write_text('{"kind": ' + "9" * 5000 + "}")
    key = _write_key(tmp_path, "key", KEY)
    assert main(["receipts", "verify", str(chain), "--key", str(key)]) == 1
    actions = tmp_path / "huge-actions.json"
    actions.write_text('{"actions": ' + "9" * 5000 + "}")
    assert main(["receipts", "export", "--actions", str(actions),
                 "--session", "session-1", "--key", str(key)]) == 1

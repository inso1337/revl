"""`deploy` and `attest` must agree on the receipt MAC's canonical bytes and on
what a key file's bytes ARE (roadmap item 873).

Two divergences were live at once, and both were the same mistake in two
places: `deploy` restated a rule `attest` already implements, and the two
readings of one rule disagreed on real input.

  * **canonicalisation.** `deploy._receipt_mac` called `json.dumps` itself and
    omitted `ensure_ascii`, so the default `True` applied and any non-ASCII
    character in the receipt body (a `--runtime-version 'python=3.14-jose\u0301'`,
    a signer label, a nonce) was `\\u`-escaped before the HMAC. `attest`'s
    documented spelling is `ensure_ascii=False`, so a third party who
    canonicalizes the way `docs/revl-attest.md` documents computed a DIFFERENT
    MAC and read a valid receipt as invalid.
  * **key loading.** `deploy` read every key off disk with
    `Path(...).read_bytes()` and no trailing-newline strip, while
    `attest.load_key` strips exactly one trailing newline. The same
    `printf 'k\\n' > k.key` file was therefore TWO keys: two `key_id`s and two
    MACs depending on which verb loaded it, so a `cat`-built key file made
    `revl deploy` refuse a chain `revl attest --verify` accepted.

Both fixes follow the precedent of item 472 (PR #851) in `erasure_receipt.py`:
`attest` is the single source of truth, and `deploy` delegates to it rather than
restating it. `deploy._load_key` is now literally `attest.load_key(str(path))`,
and `deploy._receipt_mac` MACs `attest._canonical_bytes`.

What is pinned here, one axis per section: the MAC `deploy` writes is
reproducible from `attest`'s documented spelling ALONE, and the key `deploy`
reads is the key `attest.load_key` reads, for every key file `deploy` opens
(`--key` on the chain, `--host-key` on the runner, the far host's receipt key).
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from revl import attest, deploy  # noqa: E402
from revl.errors import RevlError  # noqa: E402


SOURCE = """\
service Mail { emission[smtp] fn send(to: Str) }

extern emission fn smtp(line: Str) = @py { pass }

component Smtp provides mail: Mail {
  provide mail {
    fn send(to) = emit smtp(to)
  }
}

component Notifier requires mail: Mail {
  emit mail.send("a@b")
}
"""

#: A key with no newline, as written by an operator who used `printf`.
KEY = b"issue-873-not-a-secret"
#: The same key as `cat`/`echo` leaves it.
KEY_NEWLINE = KEY + b"\n"


def _documented_mac(body, key: bytes) -> str:
    """The MAC a third party computes from `docs/revl-attest.md` ALONE: the
    domain tag, the canonical bytes of the body with `signature` removed, and
    the key file's bytes as `attest.load_key` defines them."""
    signed = {k: v for k, v in body.items() if k != "signature"}
    return hmac.new(bytes(key),
                    deploy.RECEIPT_DOMAIN + attest._canonical_bytes(signed),
                    hashlib.sha256).hexdigest()


@pytest.fixture
def bundle(tmp_path):
    """A real `revl bundle` whose deploy attestation is signed with KEY — the
    key as `attest.load_key` reads it, which is the rule a third party
    follows."""
    from revl.bundle import build_bundle

    src = tmp_path / "mailer.rvl"
    src.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "app.revlbundle"
    build_bundle([str(src)], str(out), backends=("python",), env={})
    att = deploy.make_deploy_attestation(out, KEY, signer="ci")
    (out / deploy.ATTESTATION_NAME).write_text(
        json.dumps(att, sort_keys=True), encoding="utf-8")
    return out


@pytest.fixture
def newline_key(tmp_path):
    return tmp_path / "k.key"


@pytest.fixture
def no_newline_key(tmp_path):
    return tmp_path / "k_nonl.key"


@pytest.fixture
def other_key(tmp_path):
    return tmp_path / "other.key"


@pytest.fixture(autouse=True)
def _write_keys(newline_key, no_newline_key, other_key):
    newline_key.write_bytes(KEY_NEWLINE)
    no_newline_key.write_bytes(KEY)
    other_key.write_bytes(b"a-different-key\n")


# ---------------------------------------------------------------------------
# axis 1: one canonicalisation, and it is `attest`'s
# ---------------------------------------------------------------------------


def test_receipt_mac_is_the_mac_a_third_party_computes(bundle):
    """The headline: sign a receipt for a body carrying non-ASCII text, and the
    MAC `deploy` wrote must equal the MAC `attest`'s documented spelling
    produces. Before the fix `_receipt_mac` `\\u`-escaped this body and the two
    disagreed."""
    trust = deploy.TrustStore(keys={attest.key_id(KEY): KEY}, backend="python")
    receipt = deploy.admit(bundle, trust=trust, host_key=KEY,
                           runtime_versions={"python": "3.14-jose\u0301"})

    assert receipt["verdict"] == deploy.ACCEPT, receipt
    # The body really does carry a non-ASCII character, or this test proves
    # nothing about escaping.
    assert any(ord(c) > 0x7F for c in json.dumps(
        receipt["runtime_versions"], ensure_ascii=False))

    assert receipt["signature"] == _documented_mac(receipt, KEY)
    # ...and it is not the `ensure_ascii=True` construction, which is what the
    # bug produced. If these ever coincide again the test has gone blind.
    escaped = json.dumps({k: v for k, v in receipt.items() if k != "signature"},
                         sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    assert escaped != attest._canonical_bytes(
        {k: v for k, v in receipt.items() if k != "signature"})

    ok, why = deploy.verify_receipt(receipt, KEY)
    assert ok, why


def test_receipt_mac_delegates_to_attest_rather_than_restating_it(bundle):
    """A structural pin, not a behavioural one: the bytes `_receipt_mac` MACs
    are `attest._canonical_bytes`'s, character for character, over a body built
    to be maximally sensitive to the cheap re-spelling (non-ASCII, nested
    containers, an integer key, a float)."""
    body = {
        "kind": "revl.deploy.receipt",
        "runtime_versions": {"python": "3.14-jose\u0301", "node": "n\u00f6de"},
        "signer": "Jos\u00e9 M\u00fcller",
        "nonce": "\u2603" * 3,
        "counts": [1, {"\u00e9": 2.5}],
    }
    signed = {k: v for k, v in body.items() if k != "signature"}

    assert deploy._receipt_mac(body, KEY) == _documented_mac(body, KEY)
    assert attest._canonical_bytes(signed) == json.dumps(
        signed, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")


def test_receipt_mac_of_a_pure_ascii_body_is_unchanged(bundle):
    """The fix must not move the bytes of a body that has no non-ASCII in it:
    ASCII receipts signed before the fix still verify after it."""
    trust = deploy.TrustStore(keys={attest.key_id(KEY): KEY}, backend="python")
    receipt = deploy.admit(bundle, trust=trust, host_key=KEY,
                           runtime_versions={"python": "3.14"})
    assert receipt["signature"] == _documented_mac(receipt, KEY)
    assert receipt["signature"] == deploy._receipt_mac(
        {k: v for k, v in receipt.items() if k != "signature"}, KEY)


def test_uncanonicalizable_body_still_refuses_rather_than_signs(bundle):
    """`attest._canonical_bytes` refuses a lone surrogate; delegating means
    `deploy` inherits that refusal at the signing boundary instead of quietly
    MACing an `ensure_ascii=True` escape of it."""
    body = {"kind": "revl.deploy.receipt", "nonce": "\ud800"}
    with pytest.raises(attest.NotCanonicalizable):
        deploy._receipt_mac(body, KEY)

    trust = deploy.TrustStore(keys={attest.key_id(KEY): KEY}, backend="python")
    with pytest.raises(attest.NotCanonicalizable):
        deploy.admit(bundle, trust=trust, host_key=KEY,
                     runtime_versions={"python": "\ud800"})


# ---------------------------------------------------------------------------
# axis 2: one key rule, and it is `attest.load_key`'s
# ---------------------------------------------------------------------------


def test_load_key_is_attest_load_key(newline_key, no_newline_key, other_key):
    assert deploy._load_key(newline_key) == attest.load_key(str(newline_key))
    assert deploy._load_key(no_newline_key) == attest.load_key(
        str(no_newline_key))
    assert deploy._load_key(newline_key) == KEY
    assert deploy._load_key(no_newline_key) == KEY


def test_newline_and_no_newline_key_files_are_the_same_key(newline_key,
                                                           no_newline_key,
                                                           other_key):
    """The same secret written two ways must be ONE key: one `key_id`, and one
    MAC. A DIFFERENT secret must still be a different `key_id`, or the rule has
    been flattened into "any key works"."""
    assert attest.key_id(deploy._load_key(newline_key)) == \
        attest.key_id(deploy._load_key(no_newline_key))
    assert attest.key_id(deploy._load_key(other_key)) != \
        attest.key_id(deploy._load_key(newline_key))
    # ...and the naive read that caused the bug would have disagreed.
    assert attest.key_id(newline_key.read_bytes()) != \
        attest.key_id(deploy._load_key(newline_key))


def test_admit_bundle_chain_reads_the_key_file_by_the_documented_rule(
        bundle, newline_key, no_newline_key):
    """`revl deploy --bundle --key` used to refuse a chain signed with the
    file's own key when the file ended in a newline: 'signer-untrusted', with
    the raw bytes' `key_id` in the reason."""
    stripped = deploy.admit_bundle_chain(bundle, key_paths=[newline_key],
                                         backend="python")
    plain = deploy.admit_bundle_chain(bundle, key_paths=[no_newline_key],
                                      backend="python")
    assert stripped["verdict"] == deploy.ACCEPT, stripped
    assert plain["verdict"] == deploy.ACCEPT, plain
    assert stripped["key_id"] == plain["key_id"] == attest.key_id(KEY)


def test_admit_bundle_chain_still_refuses_a_genuinely_wrong_key(
        bundle, other_key):
    """Stripping the newline must not strip the check."""
    receipt = deploy.admit_bundle_chain(bundle, key_paths=[other_key],
                                        backend="python")
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_SIGNER


def test_serve_admit_request_reads_the_trust_key_by_the_documented_rule(
        bundle, newline_key, no_newline_key):
    wire = deploy.AdmitRequest(bundle=str(bundle), backend="python",
                               challenge="c0").to_wire()
    plain = deploy.serve_admit_request(wire, key_paths=[no_newline_key])
    newline = deploy.serve_admit_request(wire, key_paths=[newline_key])
    assert plain["receipt"]["verdict"] == deploy.ACCEPT, plain
    assert newline["receipt"]["verdict"] == deploy.ACCEPT, newline
    assert newline["receipt"]["key_id"] == plain["receipt"]["key_id"] == \
        attest.key_id(KEY)


def test_deploy_admit_command_host_key_reads_by_the_documented_rule(
        bundle, newline_key, no_newline_key):
    """`revl deploy-admit --host-key` signs its receipts with the host key. A
    host key file ending in a newline used to be signed under different bytes
    than `revl attest --verify --key` reproduces."""
    signatures = []
    for key_path in (newline_key, no_newline_key):
        request = json.dumps({
            "kind": "revl.deploy.admit-request",
            "version": "1.0",
            "bundle": str(bundle),
            "backend": "python",
            "challenge": "c0",
            "require_gauntlet": False,
            "require_conformance": False,
        })
        args = Namespace(key=[str(newline_key)], host_key=str(key_path),
                         runtime_version=["python=3.14-jose\u0301"],
                         require_gauntlet=False, require_conformance=False)
        stdin, stdout = sys.stdin, sys.stdout
        sys.stdin = io.StringIO(request + "\n")
        sys.stdout = io.StringIO()
        try:
            rc = deploy.deploy_admit_command(args)
            emitted = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = stdin, stdout
        assert rc == 0
        reply = json.loads(emitted)
        receipt = reply["receipt"]
        assert receipt["verdict"] == deploy.ACCEPT, receipt
        # The MAC is the one a third party computes from the receipt body and
        # the key file as `attest.load_key` reads it.
        assert receipt["signature"] == _documented_mac(receipt, KEY)
        signatures.append(receipt["signature"])
    # A host key file ending in a newline and one without are the SAME key: the
    # receipt a third party verifies with either file carries the same key_id.
    assert len(signatures) == 2


def test_far_host_receipt_key_reads_by_the_documented_rule(
        bundle, newline_key, no_newline_key, monkeypatch):
    """The far host's receipt key (`[processes.<p>.deploy].host_key`) is the
    fourth key `deploy` opens off disk. It must obey the same rule, so the key
    the conductor verifies the far host's receipts with is the key the far host
    signed them with, newline or no newline.

    Staging is stubbed so the test reaches the key read without an ssh channel;
    the point under test is which bytes the conductor ends up holding."""
    class _NoopTransport:
        def verify_host_key(self):
            return None

    monkeypatch.setattr(deploy.StdioRunnerTransport, "ssh",
                        classmethod(lambda cls, *a, **k: _NoopTransport()))
    monkeypatch.setattr(deploy, "stage_bundle_over_ssh",
                        lambda *a, **k: "/far/host/app.revlbundle")

    for key_path in (newline_key, no_newline_key):
        target = deploy.DeployTarget(
            process="svc", via="ssh", host="far.example", runner="revl",
            raw={"known_hosts": "/tmp/known_hosts",
                 "host_key": str(key_path),
                 "remote_bundle": "/far/host/app.revlbundle"})
        participant, problem = deploy._ssh_participant(
            target, local_bundle=bundle, backend="python",
            ssh_exe="ssh", scp_exe="scp")
        assert problem is None, problem
        assert participant._host_key == KEY

    empty = newline_key.parent / "empty.key"
    empty.write_bytes(b"")
    target = deploy.DeployTarget(
        process="svc", via="ssh", host="far.example", runner="revl",
        raw={"known_hosts": "/tmp/known_hosts",
             "host_key": str(empty),
             "remote_bundle": "/far/host/app.revlbundle"})
    participant, problem = deploy._ssh_participant(
        target, local_bundle=bundle, backend="python",
        ssh_exe="ssh", scp_exe="scp")
    assert participant is None
    assert "cannot read the far host's receipt key" in problem


def test_unreadable_and_empty_key_files_refuse_cleanly(newline_key, tmp_path):
    """`attest.load_key` refuses an empty (or newline-only) key file. Delegating
    means `deploy` inherits that refusal instead of MACing under a zero-length
    key, and it is a `RevlError`, not an `OSError`, so the callers must catch
    it."""
    empty = tmp_path / "empty.key"
    empty.write_bytes(b"")
    with pytest.raises(RevlError):
        deploy._load_key(empty)
    assert deploy._load_key(newline_key) == KEY

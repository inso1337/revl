"""`revl deploy` — the load-measured COMMIT receipt and the conductor's
comparison of it (roadmap item 118, design R2 / §1.3 step 7 / §5-A2).

`admit` (PREPARE) verifies the chain and loads nothing; the gap design R2 names
is that COMMIT then loads bytes and nothing forced the loaded bytes to equal the
verified ones, nor forced the conductor to check. These tests pin both halves of
the fix:

1. **fresh measurement, not an echo.** `commit_receipt` re-derives the IR hash
   and the artifact digest from the bytes on disk at COMMIT-load time, signs
   them with the host's own key, and fails CLOSED when the bytes cannot be
   measured.

2. **the conductor compares and refuses.** `compare_commit_receipt` checks the
   COMMIT receipt's own signature (attribution), then compares its measured
   hashes against the SIGNED admission binding and REFUSES on mismatch — R2's
   DETECTABLE case for an honest-but-buggy host that loaded the wrong bytes,
   ATTRIBUTABLE-only for a malicious one.
"""

from __future__ import annotations

import sys
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

SIGNER_KEY = b"item-118-commit-receipt-signer"
HOST_KEY = b"item-118-commit-receipt-host"
OTHER_HOST_KEY = b"item-118-commit-receipt-other-host"


@pytest.fixture
def staged(tmp_path):
    """A real `revl bundle` for the python backend plus its admission receipt —
    the ACCEPT receipt the host returned in PREPARE, which is what the COMMIT
    comparison is against."""
    from revl.bundle import build_bundle

    src = tmp_path / "mailer.rvl"
    src.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "app.revlbundle"
    build_bundle([str(src)], str(out), backends=("python",), env={})
    att = deploy.make_deploy_attestation(out, SIGNER_KEY, signer="ci")
    trust = deploy.TrustStore(keys={attest.key_id(SIGNER_KEY): SIGNER_KEY},
                              backend="python")
    admission = deploy.admit(out, trust=trust, attestation=att, host_key=HOST_KEY)
    assert admission["verdict"] == deploy.ACCEPT, admission
    return out, admission


# ---------------------------------------------------------------------------
# 1. commit_receipt: a FRESH load-time measurement, signed, fail-closed
# ---------------------------------------------------------------------------


def test_commit_receipt_measures_the_staged_bytes_afresh(staged):
    bundle, _admission = staged
    receipt = deploy.commit_receipt(bundle, backend="python", host_key=HOST_KEY)
    assert receipt["verdict"] == deploy.COMMITTED
    assert receipt["phase"] == deploy.COMMIT_PHASE
    # the hashes are RE-DERIVED from the bytes on disk, not copied from anywhere
    assert receipt["artifact_hash"] == deploy.artifact_digest(
        bundle / "emitted" / "python")
    assert receipt["composition_hash"] == attest.canonical_hash(
        deploy.staged_ir(bundle))


def test_commit_receipt_is_signed_by_the_host(staged):
    bundle, _admission = staged
    receipt = deploy.commit_receipt(bundle, backend="python", host_key=HOST_KEY)
    ok, _why = deploy.verify_receipt(receipt, HOST_KEY)
    assert ok is True
    # a different key cannot verify it — the measurement is attributable to the
    # host that made it
    bad, _why = deploy.verify_receipt(receipt, OTHER_HOST_KEY)
    assert bad is False


def test_commit_receipt_is_not_an_echo_of_admission(staged):
    """R2's whole point: the COMMIT receipt measures the bytes NOW. If the bytes
    change between admission and COMMIT, the COMMIT receipt reflects the NEW
    bytes, not the admitted hash."""
    bundle, admission = staged
    artifact = bundle / "emitted" / "python" / "components.py"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "\n# changed\n",
                        encoding="utf-8")
    receipt = deploy.commit_receipt(bundle, backend="python", host_key=HOST_KEY)
    assert receipt["artifact_hash"] != admission["artifact_hash"]


def test_commit_receipt_fails_closed_on_unmeasurable_artifact(staged):
    bundle, _admission = staged
    # a backend that was never staged has no bytes to measure
    with pytest.raises(RevlError):
        deploy.commit_receipt(bundle, backend="rust", host_key=HOST_KEY)


def test_commit_receipt_fails_closed_on_unreadable_ir(staged):
    bundle, _admission = staged
    (bundle / deploy.IR_DOCUMENT).write_text("{ not json", encoding="utf-8")
    with pytest.raises(RevlError):
        deploy.commit_receipt(bundle, backend="python", host_key=HOST_KEY)


# ---------------------------------------------------------------------------
# 2. compare_commit_receipt: the conductor's HARD gate
# ---------------------------------------------------------------------------


def test_matching_load_is_admitted(staged):
    bundle, admission = staged
    commit = deploy.commit_receipt(bundle, backend="python", host_key=HOST_KEY)
    ok, why = deploy.compare_commit_receipt(admission, commit, host_key=HOST_KEY)
    assert ok is True, why


def test_a_load_of_different_bytes_is_detected(staged):
    """The honest-but-buggy host: it was admitted to load the attested bytes but
    measured different ones at COMMIT. The conductor compares and REFUSES — R2's
    DETECTABLE case, gating COMMIT and not only PREPARE's verify."""
    bundle, admission = staged
    artifact = bundle / "emitted" / "python" / "components.py"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "\n# backdoor\n",
                        encoding="utf-8")
    commit = deploy.commit_receipt(bundle, backend="python", host_key=HOST_KEY)
    # the COMMIT receipt is perfectly authentic — the host signed it honestly
    assert deploy.verify_receipt(commit, HOST_KEY)[0] is True
    ok, why = deploy.compare_commit_receipt(admission, commit, host_key=HOST_KEY)
    assert ok is False
    assert "do not match the admitted artifact" in why


def test_a_load_of_a_different_composition_is_detected(staged):
    bundle, admission = staged
    ir_path = bundle / deploy.IR_DOCUMENT
    import json
    ir = json.loads(ir_path.read_text(encoding="utf-8"))
    ir["components"][0] = {**ir["components"][0], "name": "Smuggled"}
    ir_path.write_text(json.dumps(ir), encoding="utf-8")
    commit = deploy.commit_receipt(bundle, backend="python", host_key=HOST_KEY)
    ok, why = deploy.compare_commit_receipt(admission, commit, host_key=HOST_KEY)
    assert ok is False
    assert "composition" in why


def test_an_unattributable_commit_receipt_is_refused_not_compared(staged):
    """A COMMIT receipt the conductor cannot pin to the host's key is refused
    before any comparison: a lie must have an owner for the mechanism to mean
    anything (§5-A2)."""
    bundle, admission = staged
    commit = deploy.commit_receipt(bundle, backend="python", host_key=HOST_KEY)
    # the conductor holds the WRONG verify key for this host
    ok, why = deploy.compare_commit_receipt(admission, commit,
                                            host_key=OTHER_HOST_KEY)
    assert ok is False
    assert "unattributable" in why


def test_a_forged_measurement_is_refused(staged):
    """A COMMIT receipt whose measured hash was edited after signing fails its
    own MAC, so it never reaches the comparison."""
    bundle, admission = staged
    commit = deploy.commit_receipt(bundle, backend="python", host_key=HOST_KEY)
    forged = {**commit, "artifact_hash": "0" * 64}   # a post-signing edit
    ok, why = deploy.compare_commit_receipt(admission, forged, host_key=HOST_KEY)
    assert ok is False
    assert "unattributable" in why


def test_an_admission_receipt_cannot_stand_in_for_a_commit_receipt(staged):
    """The two records share a kind and a key but never a verdict: an ACCEPT
    admission receipt (which loaded nothing) is not a statement about what was
    loaded, so the comparison refuses it."""
    bundle, admission = staged
    ok, why = deploy.compare_commit_receipt(admission, admission,
                                            host_key=HOST_KEY)
    assert ok is False
    assert deploy.COMMITTED in why


def test_a_backend_mismatch_is_a_different_artifact(staged):
    bundle, admission = staged
    commit = deploy.commit_receipt(bundle, backend="python", host_key=HOST_KEY)
    # a COMMIT receipt claiming a different backend than the admission bound
    tampered = {k: v for k, v in commit.items() if k != "signature"}
    tampered["backend"] = "rust"
    tampered["signature"] = deploy._receipt_mac(tampered, HOST_KEY)  # re-signed
    ok, why = deploy.compare_commit_receipt(admission, tampered,
                                            host_key=HOST_KEY)
    assert ok is False
    assert "different backend" in why


def test_a_non_accept_admission_has_no_binding_to_compare(staged):
    bundle, _admission = staged
    commit = deploy.commit_receipt(bundle, backend="python", host_key=HOST_KEY)
    refused = {"kind": deploy.RECEIPT_KIND, "version": deploy.RECEIPT_VERSION,
               "verdict": deploy.REFUSE, "link": deploy.LINK_ARTIFACT}
    ok, why = deploy.compare_commit_receipt(refused, commit, host_key=HOST_KEY)
    assert ok is False
    assert deploy.ACCEPT in why

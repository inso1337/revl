"""`revl deploy` Slice 1 — attested admission, correlated seams, coordinated
rollback, co-located federation atomicity (roadmap item 118).

Four suites, one per piece of the slice, each pinning the property the design
(docs/design/118-revl-deploy.md) says is the point of that piece:

1. **re-hash on receive.** The receiver recomputes the IR hash and the artifact
   digest from the bytes staged on ITS disk and checks them against the SIGNED
   chain. An artifact whose bytes do not match its attestation is REFUSED — and
   still refused when the attestation *itself* helpfully declares the tampered
   hash, because admission never reads a self-declared `backend`/`artifact_hash`
   (Addendum 3a).

2. **peer-authenticated correlation.** A correlation envelope is authenticated
   against the peer identity, and dedup is scoped on `(peer_identity,
   composition_id, generation, idempotency_key)`. A forged envelope (a peer
   speaking under another peer's name) and a replayed one are both refused, over
   the real bridge, before the service is ever dispatched (Addendum 3b).

3. **coordinated cross-process rollback.** Two participant PROCESSES; the
   second fails at COMMIT; the first rolls back with no residue. The proof that
   this is a coordinated protocol and not a lift of `apply.py`'s in-process
   theorem is read off the WAL: the unwind records carry the PARTICIPANT's pid,
   not the conductor's. A participant the conductor cannot reach is reported
   `unresolved`, never `rolled-back`.

4. **federation atomicity.** A plan that necessarily crosses a non-deferrable
   irreversible effect is REFUSED admission; a stranded participant rolls
   forward or back per the durable record, and fails closed when the record
   cannot be read.
"""

from __future__ import annotations

import asyncio
import builtins
import importlib.util
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from revl import attest, deploy  # noqa: E402
from revl import placement as _placement  # noqa: E402


def _bridge():
    """backends/python/bridge.py, imported by path (it needs no cordis). Same
    registration dance tests/test_seam_deadlines.py documents (py3.14
    dataclass annotation resolution)."""
    name = "revl_deploy_test_bridge"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "backends" / "python" / "bridge.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# a staged bundle to admit
# ---------------------------------------------------------------------------

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

SIGNER_KEY = b"item-118-slice-1-signer"
HOST_KEY = b"item-118-slice-1-host"


@pytest.fixture
def staged(tmp_path):
    """A real `revl bundle` staged for the python backend, plus the deploy
    attestation over its chain. Everything admission compares is re-derived from
    these bytes."""
    from revl.bundle import build_bundle

    src = tmp_path / "mailer.rvl"
    src.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "app.revlbundle"
    build_bundle([str(src)], str(out), backends=("python",), env={})
    att = deploy.make_deploy_attestation(out, SIGNER_KEY, signer="ci")
    return out, att


def _trust(**over):
    base = {"keys": {attest.key_id(SIGNER_KEY): SIGNER_KEY}, "backend": "python"}
    base.update(over)
    return deploy.TrustStore(**base)


# ---------------------------------------------------------------------------
# 1. the attestation chain, verified at admission, re-hashed on receive
# ---------------------------------------------------------------------------


def test_chain_binds_every_link(staged):
    bundle, att = staged
    bindings = att["evidence_bindings"]
    assert deploy.artifact_facet("python") in bindings
    assert deploy.FACET_POLICY in bindings
    assert deploy.FACET_LOCK in bindings
    # the bindings are inside the SIGNED body: tampering with one breaks the
    # signature, so the chain is committed by one verification.
    forged = dict(att)
    forged["evidence_bindings"] = {**bindings, deploy.FACET_LOCK: "0" * 64}
    ok, _reason = attest.verify_attestation(forged, SIGNER_KEY)
    assert ok is False


def test_admit_accepts_the_attested_bundle(staged):
    bundle, att = staged
    receipt = deploy.admit(bundle, trust=_trust(), attestation=att,
                           host_key=HOST_KEY,
                           runtime_versions={"python": sys.version.split()[0]})
    assert receipt["verdict"] == deploy.ACCEPT, receipt
    # the receipt binds the RE-HASHED values, not anything the sender said
    assert receipt["artifact_hash"] == deploy.artifact_digest(
        bundle / "emitted" / "python")
    assert receipt["composition_hash"] == attest.canonical_hash(
        deploy.staged_ir(bundle))
    ok, _why = deploy.verify_receipt(receipt, HOST_KEY)
    assert ok is True


def test_tampered_artifact_bytes_are_refused_on_receive(staged):
    """The headline binding rule: the receiver re-hashes the bytes it will
    execute. A perfectly authentic attestation over DIFFERENT bytes is refused."""
    bundle, att = staged
    artifact = bundle / "emitted" / "python" / "components.py"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "\n# backdoor\n",
                        encoding="utf-8")
    receipt = deploy.admit(bundle, trust=_trust(), attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_ARTIFACT
    assert "bytes in hand" in receipt["reason"]


def test_self_declared_artifact_hash_is_never_trusted(staged):
    """A sender that also writes its OWN `backend`/`artifact_hash` into the
    signed body, agreeing with the tampered bytes, changes nothing: admission
    reads only the bound facet and compares it to its own re-hash."""
    bundle, att = staged
    artifact = bundle / "emitted" / "python" / "components.py"
    artifact.write_text("# entirely different artifact\n", encoding="utf-8")
    tampered_digest = deploy.artifact_digest(bundle / "emitted" / "python")
    body = {k: v for k, v in att.items() if k != attest.SIGNATURE_FIELD}
    body["backend"] = "python"
    body["artifact_hash"] = tampered_digest      # a self-declared, SIGNED lie
    lying = {**body, attest.SIGNATURE_FIELD: attest._sign(body, SIGNER_KEY)}
    # the lying attestation is authentic — the signature verifies
    assert attest.verify_attestation(lying, SIGNER_KEY)[0] is True
    receipt = deploy.admit(bundle, trust=_trust(), attestation=lying)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_ARTIFACT
    # and the members it declared about itself are the ones admission refuses
    # to read at all
    assert set(deploy.SELF_DECLARED_IGNORED) >= {"backend", "artifact_hash"}


def test_tampered_ir_is_refused_on_receive(staged):
    bundle, att = staged
    ir_path = bundle / deploy.IR_DOCUMENT
    ir = json.loads(ir_path.read_text(encoding="utf-8"))
    ir["components"][0] = {**ir["components"][0], "name": "Smuggled"}
    ir_path.write_text(json.dumps(ir), encoding="utf-8")
    receipt = deploy.admit(bundle, trust=_trust(), attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_COMPOSITION


def test_tampered_attestation_is_a_signature_refusal(staged):
    bundle, att = staged
    forged = {**att, "guarantees": ["G1"]}
    receipt = deploy.admit(bundle, trust=_trust(), attestation=forged)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_SIGNATURE


def test_untrusted_and_revoked_signers_are_refused(staged):
    bundle, att = staged
    empty = deploy.TrustStore(keys={}, backend="python")
    assert deploy.admit(bundle, trust=empty, attestation=att)["link"] == deploy.LINK_SIGNER
    kid = attest.key_id(SIGNER_KEY)
    revoked = _trust(revoked=frozenset({kid}))
    receipt = deploy.admit(bundle, trust=revoked, attestation=att)
    assert receipt["link"] == deploy.LINK_SIGNER
    assert "REVOKED" in receipt["reason"]


def test_a_backend_the_chain_does_not_bind_is_refused(staged):
    bundle, att = staged
    receipt = deploy.admit(bundle, trust=_trust(backend="rust"), attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_BACKEND


def test_capability_expansion_beyond_the_ceiling_is_refused(staged):
    bundle, att = staged
    policy = json.loads((bundle / deploy.POLICY_NAME).read_text(encoding="utf-8"))
    assert policy["capabilities"], "the fixture must cross a capability"
    receipt = deploy.admit(bundle, trust=_trust(capability_ceiling=frozenset()),
                           attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_CAPABILITY
    # inside the ceiling it admits
    wide = _trust(capability_ceiling=frozenset(policy["capabilities"]))
    assert deploy.admit(bundle, trust=wide, attestation=att)["verdict"] == deploy.ACCEPT


@pytest.mark.parametrize("label,written", [
    # every way roadmap 428 F4 found of making the DERIVED projection say
    # nothing, each one signed over as staged so the chain still verifies end
    # to end and the `policy` facet still re-hashes.
    ("absent", None),
    ("malformed", "{not json at all"),
    ("relabelled", '{"emissions": 1, "caps": ["smtp"]}'),
    ("emptied", '{"capabilities": [], "emissions": 1}'),
])
def test_the_ceiling_is_measured_off_the_ir_not_off_policy_json(
        staged, tmp_path, label, written):
    """Roadmap 428 F4. `policy.json` is a PROJECTION `build_bundle` derives from
    the IR, and the party a ceiling constrains is the party that writes it. So
    the ceiling is measured off `ir/ir.json`, re-hashed against the signed
    `composition_hash` two steps earlier, and moving, emptying, corrupting or
    deleting the projection does not move it."""
    bundle, _att = staged
    target = tmp_path / f"tampered-{label}"
    shutil.copytree(bundle, target)
    policy_path = target / deploy.POLICY_NAME
    real = set(json.loads(policy_path.read_text(encoding="utf-8"))["capabilities"])
    assert real, "the fixture must cross a capability"
    if written is None:
        policy_path.unlink()
    else:
        policy_path.write_text(written, encoding="utf-8")

    # sign the chain over the bundle AS TAMPERED: this is not a forgery, it is
    # what a deploying operator's own `bundle` + sign run produces.
    att = deploy.make_deploy_attestation(target, SIGNER_KEY, signer="ci")
    assert attest.verify_attestation(att, SIGNER_KEY)[0] is True
    assert attest.canonical_hash(deploy.staged_ir(target)) == att["composition_hash"]

    receipt = deploy.admit(target, trust=_trust(capability_ceiling=frozenset()),
                           attestation=att)
    assert receipt["verdict"] == deploy.REFUSE, receipt
    assert receipt["link"] == deploy.LINK_CAPABILITY
    assert sorted(real)[0] in receipt["reason"]
    # and the authority the refusal was measured against is the IR, whose
    # derived surface is untouched by anything done to the projection.
    assert deploy.capability_surface(deploy.staged_ir(target))[0] == real


def test_a_regenerated_policy_json_does_not_move_the_ceiling(staged, tmp_path):
    """The projection is not merely tamper-evident by hash: REGENERATING it (so
    the binding is honest, the signature is fresh and every re-hash agrees) over
    a narrowed capability list still does not widen what the receiver admits."""
    bundle, _att = staged
    target = tmp_path / "regenerated"
    shutil.copytree(bundle, target)
    policy_path = target / deploy.POLICY_NAME
    real = set(json.loads(policy_path.read_text(encoding="utf-8"))["capabilities"])
    policy_path.write_text(
        json.dumps({"capabilities": [], "emissions": 0}, indent=2, sort_keys=True)
        + "\n", encoding="utf-8")
    att = deploy.make_deploy_attestation(target, SIGNER_KEY, signer="ci")
    # the regenerated projection IS the bound one: the facet re-hash passes.
    assert att["evidence_bindings"][deploy.FACET_POLICY] == \
        deploy._file_digest(policy_path)
    receipt = deploy.admit(target, trust=_trust(capability_ceiling=frozenset()),
                           attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_CAPABILITY
    assert sorted(real)[0] in receipt["reason"]


def test_a_projection_that_disagrees_with_the_ir_is_refused(staged, tmp_path):
    """Even INSIDE the ceiling, a bound `policy.json` that does not project the
    admitted IR is a tamper signal, not a pass. The ceiling is not measured from
    it, so this refusal is about the chain's honesty rather than about
    authority."""
    bundle, _att = staged
    target = tmp_path / "disagreeing"
    shutil.copytree(bundle, target)
    policy_path = target / deploy.POLICY_NAME
    real = set(json.loads(policy_path.read_text(encoding="utf-8"))["capabilities"])
    policy_path.write_text(json.dumps({"capabilities": ["something-else"],
                                       "emissions": 1}), encoding="utf-8")
    att = deploy.make_deploy_attestation(target, SIGNER_KEY, signer="ci")
    wide = _trust(capability_ceiling=frozenset(real | {"something-else"}))
    receipt = deploy.admit(target, trust=wide, attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_POLICY
    assert "does not project" in receipt["reason"]


def test_the_ceiling_fails_closed_when_the_surface_cannot_be_measured(
        staged, monkeypatch):
    """Item 428 F11's fail-open shape, checked here and CLOSED: an unmeasurable
    capability surface is refused, never read as an empty one."""
    from revl.errors import RevlError

    bundle, att = staged

    def _explode(_ir):
        raise RevlError(deploy.IR_DOCUMENT, 0, "boundary analysis is unavailable")

    monkeypatch.setattr(deploy, "capability_surface", _explode)
    receipt = deploy.admit(bundle, trust=_trust(capability_ceiling=frozenset()),
                           attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_CAPABILITY
    assert "cannot measure" in receipt["reason"]

    # and the derivation itself refuses rather than returning an empty set when
    # the IR it is handed cannot be analysed.
    monkeypatch.undo()
    with pytest.raises(RevlError):
        deploy.capability_surface({"components": "not a list"})


def test_stale_evidence_is_refused_even_with_a_valid_signature(staged):
    bundle, _att = staged
    old = deploy.make_deploy_attestation(bundle, SIGNER_KEY,
                                         now="2020-01-01T00:00:00+00:00")
    trust = _trust(evidence_ttl_seconds=60.0)
    receipt = deploy.admit(bundle, trust=trust, attestation=old)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_EVIDENCE
    assert attest.verify_attestation(old, SIGNER_KEY)[0] is True  # authentic, stale


# ------------------------------- freshness is bounded at BOTH ends (428 F7)
#
# The timestamp is signer-chosen and `attest._now_iso` passes an ISO string
# straight through, so post-dating is a one-argument change. A TTL bounds only
# the past: a 2099 stamp has a NEGATIVE age, which is not greater than any TTL.


def test_a_post_dated_attestation_is_refused(staged):
    bundle, _att = staged
    future = deploy.make_deploy_attestation(bundle, SIGNER_KEY,
                                            now="2099-01-01T00:00:00+00:00")
    assert attest.verify_attestation(future, SIGNER_KEY)[0] is True
    receipt = deploy.admit(bundle, trust=_trust(evidence_ttl_seconds=3600.0),
                           attestation=future)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_EVIDENCE
    assert "FUTURE" in receipt["reason"]


def test_clock_skew_inside_the_tolerance_still_admits(staged):
    """The future bound is a tolerance for two clocks disagreeing, not a window
    the signer gets to choose, so it is small and it is the receiver's."""
    bundle, _att = staged
    trust = _trust(evidence_ttl_seconds=3600.0)
    stamp = datetime(2001, 9, 9, 1, 46, 40, tzinfo=timezone.utc)
    signed_at = stamp.timestamp()
    att = deploy.make_deploy_attestation(bundle, SIGNER_KEY, now=stamp)
    inside = signed_at - (trust.clock_skew_seconds / 2)
    assert deploy.admit(bundle, trust=trust, attestation=att,
                        now=inside)["verdict"] == deploy.ACCEPT
    outside = signed_at - (trust.clock_skew_seconds + 60)
    receipt = deploy.admit(bundle, trust=trust, attestation=att, now=outside)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_EVIDENCE


def test_the_receiver_may_anchor_freshness_on_its_own_instant(staged):
    """`not_before` is the anchor the receiver SUPPLIES rather than reads off
    the record: evidence from before the last state it accepted is refused,
    whatever the signer stamped and whatever the TTL allows."""
    bundle, _att = staged
    stamp = datetime(2001, 9, 9, 1, 46, 40, tzinfo=timezone.utc)
    signed_at = stamp.timestamp()
    att = deploy.make_deploy_attestation(bundle, SIGNER_KEY, now=stamp)
    assert deploy.admit(bundle, trust=_trust(not_before=signed_at - 1000),
                        attestation=att, now=signed_at + 100
                        )["verdict"] == deploy.ACCEPT
    receipt = deploy.admit(bundle, trust=_trust(not_before=signed_at + 500),
                           attestation=att, now=signed_at + 600)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_EVIDENCE
    assert "predates this receiver's own freshness anchor" in receipt["reason"]


def test_an_unreadable_freshness_anchor_fails_closed(staged):
    bundle, att = staged
    receipt = deploy.admit(bundle, trust=_trust(not_before="not an instant"),
                           attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_EVIDENCE
    assert "refused fail-closed" in receipt["reason"]


def test_cross_domain_deploy_on_a_symmetric_signature_is_refused(staged):
    """§2.4 / §5-A1: a verifier that holds the HMAC secret is also a forger, so
    `signer untrusted` would be a fiction across a trust domain. Slice 1 refuses
    rather than shipping the fiction; the Ed25519 upgrade is the unlock."""
    bundle, att = staged
    receipt = deploy.admit(bundle, trust=_trust(cross_domain=True), attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_SIGNER
    assert "Ed25519" in receipt["reason"]


def test_conformance_cert_binding_is_rehashed(staged):
    bundle, _att = staged
    cert_dir = bundle / "conformance"
    cert_dir.mkdir()
    cert = cert_dir / "python.json"
    cert.write_text('{"tier": "python", "pass": true}\n', encoding="utf-8")
    att = deploy.make_deploy_attestation(bundle, SIGNER_KEY)
    assert deploy.conformance_facet("python") in att["evidence_bindings"]
    assert deploy.admit(bundle, trust=_trust(), attestation=att)["verdict"] == deploy.ACCEPT
    cert.write_text('{"tier": "python", "pass": false}\n', encoding="utf-8")
    receipt = deploy.admit(bundle, trust=_trust(), attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_EVIDENCE


def test_a_receiver_may_require_conformance_evidence_to_exist(staged):
    """The conformance facet's `unbound-means-unchecked` shape (roadmap 428 F4,
    filed STILL OPEN): a bundle built without the six-tier suite binds no
    `conformance/<backend>` facet, so the re-hash check has nothing to bite on
    and the missing runtime-target evidence is silently unchecked. Off by
    default an honestly degraded build still admits (the same honest-degrade the
    gauntlet facet gets); a receiver that demands the evidence refuses the chain
    that carries none."""
    bundle, att = staged
    # a plain `revl bundle` stages no conformance cert, so the facet is absent
    assert deploy.conformance_facet("python") not in att["evidence_bindings"]
    # off by default: unbound stays unchecked, the bundle admits
    assert deploy.admit(bundle, trust=_trust(),
                        attestation=att)["verdict"] == deploy.ACCEPT
    receipt = deploy.admit(bundle, trust=_trust(require_conformance=True),
                           attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_EVIDENCE
    assert "requires item-306 conformance evidence" in receipt["reason"]


def test_require_conformance_admits_when_the_cert_is_bound(staged):
    """`require_conformance` demands the facet be PRESENT, not that a fresh cert
    be produced: a chain that binds `conformance/<backend>` (re-hashed here)
    satisfies the requirement and the bundle admits."""
    bundle, _att = staged
    cert_dir = bundle / "conformance"
    cert_dir.mkdir()
    (cert_dir / "python.json").write_text(
        '{"tier": "python", "pass": true}\n', encoding="utf-8")
    att = deploy.make_deploy_attestation(bundle, SIGNER_KEY, signer="ci")
    assert deploy.conformance_facet("python") in att["evidence_bindings"]
    assert deploy.admit(bundle, trust=_trust(require_conformance=True),
                        attestation=att)["verdict"] == deploy.ACCEPT
    # and a require_conformance receiver whose backend the chain did NOT attest
    # (a cert bound for another backend only) still refuses on its own backend
    other = deploy.TrustStore(keys={attest.key_id(SIGNER_KEY): SIGNER_KEY},
                              backend="typescript", require_conformance=True)
    receipt = deploy.admit(bundle, trust=other, attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    # refuses at `backend` first — there is no typescript artifact at all here —
    # which is the earlier chain link, so the guarantee is unbroken either way
    assert receipt["link"] in (deploy.LINK_BACKEND, deploy.LINK_EVIDENCE)


# ------------------------------------------- the gauntlet VERDICT (428 F8)
#
# The facet was hashed and never read, so `admit` accepted a bundle carrying
# its own evidence that it should not have been built, and accepted a genuine
# `admissible` dossier graded over a DIFFERENT composition, because the
# dossier named no composition at all.


def _restage(bundle, dossier):
    """Write `dossier` as the staged gauntlet evidence and re-sign the chain
    over it, so nothing in these tests is a forgery: the attestation is honest
    about exactly the bytes on disk."""
    (bundle / "gauntlet.json").write_text(
        json.dumps(dossier, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return deploy.make_deploy_attestation(bundle, SIGNER_KEY, signer="ci")


def _dossier(bundle):
    return json.loads((bundle / "gauntlet.json").read_text(encoding="utf-8"))


def test_the_staged_gauntlet_dossier_names_the_composition_it_graded(staged):
    bundle, att = staged
    dossier = _dossier(bundle)
    assert dossier["verdict"] == deploy.GAUNTLET_ADMISSIBLE
    assert dossier[deploy.GAUNTLET_IDENTITY] == att["composition_hash"]


@pytest.mark.parametrize("verdict", ["rejected", "", None, "ADMISSIBLE"])
def test_a_dossier_whose_verdict_is_not_admissible_is_refused(staged, verdict):
    bundle, _att = staged
    dossier = _dossier(bundle)
    dossier["verdict"] = verdict
    receipt = deploy.admit(bundle, trust=_trust(),
                           attestation=_restage(bundle, dossier))
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.FACET_GAUNTLET
    assert "not 'admissible'" in receipt["reason"]


def test_an_admissible_dossier_graded_over_another_composition_is_refused(
        staged, tmp_path):
    """The second half of F8: the record is genuine, `admissible`, and hashes
    correctly into the chain. It is simply not evidence about THIS artifact."""
    from revl.bundle import build_bundle

    other_src = tmp_path / "other.rvl"
    other_src.write_text(
        "service Log { emission[pr] fn w(m: Str) }\n"
        "extern emission fn pr(m: Str) = @py { pass }\n"
        "component L provides log: Log { provide log { fn w(m) = emit pr(m) } }\n"
        "component U requires log: Log { emit log.w(\"x\") }\n",
        encoding="utf-8")
    other = tmp_path / "other.revlbundle"
    build_bundle([str(other_src)], str(other), backends=("python",), env={})
    foreign = _dossier(other)
    assert foreign["verdict"] == deploy.GAUNTLET_ADMISSIBLE

    bundle, _att = staged
    receipt = deploy.admit(bundle, trust=_trust(),
                           attestation=_restage(bundle, foreign))
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.FACET_GAUNTLET
    assert "graded over composition" in receipt["reason"]


def test_a_dossier_naming_no_composition_is_refused_not_assumed(staged):
    """Fail closed: an unidentified dossier is not evidence about this
    composition, so it is refused rather than taken to be about it."""
    bundle, _att = staged
    dossier = _dossier(bundle)
    del dossier[deploy.GAUNTLET_IDENTITY]
    receipt = deploy.admit(bundle, trust=_trust(),
                           attestation=_restage(bundle, dossier))
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.FACET_GAUNTLET
    assert "names no composition" in receipt["reason"]


def test_an_unreadable_dossier_is_refused_not_read_as_a_pass(staged):
    """Fail closed on the input itself, item 428 F11's shape: bytes the
    receiver cannot parse are a refusal, never an absent facet."""
    bundle, _att = staged
    (bundle / "gauntlet.json").write_text("{not json at all", encoding="utf-8")
    att = deploy.make_deploy_attestation(bundle, SIGNER_KEY, signer="ci")
    receipt = deploy.admit(bundle, trust=_trust(), attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.FACET_GAUNTLET
    assert "cannot be read as JSON" in receipt["reason"]


def test_a_receiver_may_require_gauntlet_evidence_to_exist(staged):
    """Deleting the dossier and re-signing removes the facet, so the verdict
    check has nothing to bite on. A receiver that demands the evidence refuses
    the chain that carries none."""
    bundle, _att = staged
    (bundle / "gauntlet.json").unlink()
    att = deploy.make_deploy_attestation(bundle, SIGNER_KEY, signer="ci")
    assert deploy.FACET_GAUNTLET not in att["evidence_bindings"]
    # off by default, an honestly degraded build still admits
    assert deploy.admit(bundle, trust=_trust(),
                        attestation=att)["verdict"] == deploy.ACCEPT
    receipt = deploy.admit(bundle, trust=_trust(require_gauntlet=True),
                           attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.FACET_GAUNTLET


# ---------------------- what the artifact digest is a statement about (428 F11)


def _resign_artifact(bundle):
    """Re-sign the chain over the artifact tree as it stands. Used to make the
    receiving half of each case honest: the attestation binds exactly the
    digest of the tree at signing time, so nothing below is a forgery."""
    return deploy.make_deploy_attestation(bundle, SIGNER_KEY, signer="ci")


def test_an_empty_artifact_tree_is_refused_not_digested(staged):
    """It folds nothing and yields sha256(b""), a value anyone can produce
    without holding an artifact at all. Signing it is refused, and so is
    admitting one against a chain signed over the real bytes."""
    import hashlib

    bundle, att = staged
    emitted = bundle / "emitted" / "python"
    saved = {p.name: p.read_bytes() for p in emitted.iterdir()}
    for p in list(emitted.iterdir()):
        p.unlink()

    with pytest.raises(Exception) as signing:
        _resign_artifact(bundle)
    assert "empty" in str(signing.value)
    assert hashlib.sha256(b"").hexdigest()[:12] in str(signing.value)

    receipt = deploy.admit(bundle, trust=_trust(), attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_ARTIFACT

    for name, data in saved.items():
        (emitted / name).write_bytes(data)
    assert deploy.admit(bundle, trust=_trust(),
                        attestation=att)["verdict"] == deploy.ACCEPT


def test_a_symlink_in_the_artifact_tree_is_refused_not_followed(staged, tmp_path):
    """`p.is_file()` followed links, so bound bytes could live outside the
    bundle and stay writable after signing. A digest over a path the bundle
    does not own is not a statement about the bundle."""
    bundle, _att = staged
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = outside / "extra.py"
    payload.write_text("VALUE = 1\n", encoding="utf-8")

    link = bundle / "emitted" / "python" / "extra.py"
    os.symlink(payload, link)
    with pytest.raises(Exception) as signing:
        _resign_artifact(bundle)
    assert "symlink" in str(signing.value)

    # and on the receiving side, against a chain signed over the real tree
    link.unlink()
    att = _resign_artifact(bundle)
    os.symlink(payload, link)
    receipt = deploy.admit(bundle, trust=_trust(), attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_ARTIFACT
    assert "symlink" in receipt["reason"]


def test_a_symlinked_backend_directory_is_refused(staged, tmp_path):
    """Same defect one level up: `emitted/<backend>/` itself a link out of the
    bundle, so the whole artifact lives somewhere the bundle does not own."""
    bundle, att = staged
    real = bundle / "emitted" / "python"
    moved = tmp_path / "moved-emitted"
    shutil.move(str(real), str(moved))
    os.symlink(moved, real)
    receipt = deploy.admit(bundle, trust=_trust(), attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_ARTIFACT
    assert "symlink" in receipt["reason"]


def test_repin_closes_the_window_between_admission_and_load(staged):
    """`admit` is PREPARE and loads nothing, so the ACCEPT receipt described
    bytes that could change or vanish before anything executed. `repin` is the
    check a loader runs immediately before it runs them."""
    bundle, att = staged
    receipt = deploy.admit(bundle, trust=_trust(), attestation=att)
    assert receipt["verdict"] == deploy.ACCEPT
    assert deploy.repin(bundle, receipt) == (
        True, "the staged bytes are still the admitted ones")

    artifact = bundle / "emitted" / "python" / "components.py"
    original = artifact.read_bytes()
    artifact.write_bytes(original + b"\n# swapped after admission\n")
    ok, reason = deploy.repin(bundle, receipt)
    assert ok is False
    assert "changed since admission" in reason

    artifact.unlink()
    ok, reason = deploy.repin(bundle, receipt)
    assert ok is False
    assert "no longer be digested" in reason
    artifact.write_bytes(original)

    ir_path = bundle / "ir" / "ir.json"
    saved_ir = ir_path.read_bytes()
    doc = json.loads(saved_ir)
    doc["swappedAfterAdmission"] = True
    ir_path.write_text(json.dumps(doc), encoding="utf-8")
    ok, reason = deploy.repin(bundle, receipt)
    assert ok is False
    assert "changed since admission" in reason
    ir_path.write_bytes(saved_ir)


def test_repin_refuses_a_receipt_that_authorises_nothing(staged):
    """Fail closed: a REFUSE receipt, and one with no hashes, authorise no
    load at all, so neither is treated as a pass."""
    bundle, _att = staged
    refused = deploy._refusal(deploy.LINK_ARTIFACT, "no")
    ok, reason = deploy.repin(bundle, refused)
    assert ok is False
    assert "nothing it authorises loading" in reason

    ok, reason = deploy.repin(bundle, {"verdict": deploy.ACCEPT})
    assert ok is False
    assert "nothing to re-pin against" in reason


# ------------------------------- a crash is not a refusal (428 F10)
#
# `json.loads` builds a lone surrogate out of a `"\ud800"` escape and
# `str.encode("utf-8")` then refuses it, so a peer-supplied record made the
# canonical spelling raise `UnicodeEncodeError` straight through contracts
# whose whole point is to answer `(ok, reason)`. A crash escapes past every
# caller written to read a verdict.

LONE_SURROGATE = "\ud800"


def test_a_lone_surrogate_is_refused_not_raised_by_verify_attestation():
    key = b"k"
    att = {"kind": "revl.attestation", "version": "2.0",
           "signer": LONE_SURROGATE, "composition_hash": "0" * 64,
           "verdict": "admitted", "key_id": attest.key_id(key),
           "signature": "00"}
    ok, reason = attest.verify_attestation(att, key)
    assert ok is False
    assert "no canonical byte spelling" in reason


def test_a_lone_surrogate_in_the_signer_refuses_admission(staged, tmp_path):
    bundle, att = staged
    att = dict(att)
    att["signer"] = LONE_SURROGATE
    receipt = deploy.admit(bundle, trust=_trust(), attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_SIGNATURE


def test_a_staged_ir_that_cannot_be_canonicalized_refuses(staged):
    """The receiver's OWN input, one step later: `ir/ir.json` is read with
    `json.loads`, so a `\\ud800` escape in it survives to `canonical_hash`.
    An unmeasurable composition refuses rather than crashing."""
    bundle, att = staged
    ir = json.loads((bundle / "ir" / "ir.json").read_text(encoding="utf-8"))
    ir["signerNote"] = LONE_SURROGATE
    (bundle / "ir" / "ir.json").write_text(
        json.dumps(ir, ensure_ascii=True), encoding="utf-8")
    receipt = deploy.admit(bundle, trust=_trust(), attestation=att)
    assert receipt["verdict"] == deploy.REFUSE
    assert receipt["link"] == deploy.LINK_COMPOSITION
    assert "cannot be canonically hashed" in receipt["reason"]


def test_a_lone_surrogate_in_a_peer_envelope_is_malformed_not_a_crash():
    """`_envelope_bytes` had the identical shape and is reachable from
    `CorrelationGuard.admit` on ANY peer-supplied envelope, which makes the
    crash a denial of service on the seam rather than only a verifier bug."""
    guard = deploy.CorrelationGuard({"peer": b"s"})
    wire = deploy.seal(_envelope("peer"), b"s")
    wire["realm"] = LONE_SURROGATE
    ok, reason = guard.admit(wire)
    assert ok is False
    assert reason == deploy.REJECT_MALFORMED


def test_a_receipt_that_cannot_be_canonicalized_is_refused_not_raised():
    receipt = {"kind": deploy.RECEIPT_KIND, "version": deploy.RECEIPT_VERSION,
               "verdict": deploy.ACCEPT, "nonce": object(), "signature": "00"}
    ok, reason = deploy.verify_receipt(receipt, b"host")
    assert ok is False
    assert "cannot be verified" in reason


# ---------------------------------------------------------------------------
# 2. the correlation envelope, authenticated against the peer identity
# ---------------------------------------------------------------------------

DB_SECRET = b"secret-for-db"
EDGE_SECRET = b"secret-for-edge"


def _envelope(identity, *, effect="e1", key="idem-1", generation=7):
    return deploy.Correlation(composition_id="app", generation=generation,
                              peer_identity=identity, effect_id=effect,
                              realm="prod", idempotency_key=key)


def _guard():
    return deploy.CorrelationGuard({"db": DB_SECRET, "edge": EDGE_SECRET})


def test_a_sealed_envelope_from_the_right_peer_is_admitted():
    guard = _guard()
    ok, reason = guard.admit(deploy.seal(_envelope("edge"), EDGE_SECRET))
    assert ok is True, reason


def test_an_envelope_forged_under_another_peers_name_is_rejected():
    """`edge` seals an envelope but claims to be `db`. The tag does not verify
    under db's secret, so the claim is refused — identity is proven, not
    asserted."""
    guard = _guard()
    forged = deploy.seal(_envelope("db"), EDGE_SECRET)
    ok, reason = guard.admit(forged)
    assert (ok, reason) == (False, deploy.REJECT_FORGED)


def test_an_unknown_peer_is_rejected_not_trusted():
    guard = _guard()
    ok, reason = guard.admit(deploy.seal(_envelope("ghost"), b"whatever"))
    assert (ok, reason) == (False, deploy.REJECT_UNKNOWN_PEER)


def test_a_valid_tag_from_the_wrong_tls_session_is_rejected():
    """Both halves are required: the per-process secret AND the identity the
    transport authenticated. A leaked secret cannot speak from another session."""
    guard = _guard()
    sealed = deploy.seal(_envelope("db"), DB_SECRET)
    ok, reason = guard.admit(sealed, transport_identity="edge")
    assert (ok, reason) == (False, deploy.REJECT_PEER_MISMATCH)
    assert guard.admit(sealed, transport_identity="db")[0] is True


def test_a_replayed_envelope_is_rejected():
    guard = _guard()
    sealed = deploy.seal(_envelope("db"), DB_SECRET)
    assert guard.admit(sealed)[0] is True
    ok, reason = guard.admit(sealed)
    assert (ok, reason) == (False, deploy.REJECT_DUPLICATE)


def test_dedup_is_scoped_by_peer_identity():
    """The same idempotency key from two DIFFERENT peers is two different
    crossings: the scope is `(peer_identity, composition_id, generation,
    idempotency_key)`, so one peer cannot poison another's key namespace."""
    guard = _guard()
    assert guard.admit(deploy.seal(_envelope("db"), DB_SECRET))[0] is True
    assert guard.admit(deploy.seal(_envelope("edge"), EDGE_SECRET))[0] is True
    assert _envelope("db").dedup_key() == ("db", "app", 7, "idem-1")


def test_a_different_generation_is_not_a_replay():
    guard = _guard()
    assert guard.admit(deploy.seal(_envelope("db", generation=1), DB_SECRET))[0] is True
    assert guard.admit(deploy.seal(_envelope("db", generation=2), DB_SECRET))[0] is True


# --- the same rules, over the real bridge seam ------------------------------


class _Ctx:
    def __init__(self, table):
        self._table = table

    def get(self, key):
        return self._table.get(key)


class _Counter:
    """A service that records every dispatch, so a test can prove a refused
    envelope never reached it."""

    def __init__(self):
        self.calls = []

    def get(self, name):
        self.calls.append(name)
        return f"v:{name}"


@pytest.fixture
def sockdir():
    """A short-pathed directory for Unix sockets (the macOS ``sun_path`` limit;
    same rationale as tests/test_seam_deadlines.py)."""
    import shutil
    import tempfile

    directory = tempfile.mkdtemp(prefix="rvdp", dir="/tmp")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


class _Seam:
    """A `bridge.serve` provider on its own event loop thread, shut down
    cleanly (server closed, handler tasks cancelled, loop closed) so a stopped
    seam leaves no unraisable behind."""

    def __init__(self, bridge, sock, service, correlation=None):
        self.bridge, self.sock, self.service = bridge, sock, service
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self._loop)
            server = self._loop.run_until_complete(bridge.serve(
                _Ctx({"Cache": service}), {"Cache": ["get"]}, sock,
                correlation=correlation))
            ready.set()
            self._loop.run_forever()
            server.close()
            self._loop.run_until_complete(server.wait_closed())
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            self._loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert ready.wait(10)

    def stop(self):
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


@pytest.fixture
def seam(sockdir):
    """A UDS bridge seam whose provider runs a correlation guard."""
    bridge = _bridge()
    service = _Counter()
    guard = _guard()
    running = _Seam(bridge, str(sockdir / "s.sock"), service, correlation=guard)
    try:
        yield bridge, running.sock, service, guard
    finally:
        running.stop()


def test_seam_call_carries_and_authenticates_the_correlation_envelope(seam):
    bridge, sock, service, _guard_obj = seam
    counter = {"n": 0}

    def envelope(key, method):
        counter["n"] += 1
        return deploy.seal(_envelope("db", effect=f"{key}.{method}",
                                     key=f"k{counter['n']}"), DB_SECRET)

    client = bridge._Client(sock, correlation=envelope)
    try:
        assert client.call("Cache", "get", ["a"]) == "v:a"
        assert service.calls == ["a"]
    finally:
        client.close()


def test_a_forged_envelope_is_refused_before_the_service_is_dispatched(seam):
    bridge, sock, service, _guard_obj = seam
    forged = deploy.seal(_envelope("db"), EDGE_SECRET)   # edge speaking as db
    client = bridge._Client(sock, correlation=forged)
    try:
        with pytest.raises(RuntimeError) as caught:
            client.call("Cache", "get", ["a"])
        assert deploy.REJECT_FORGED in str(caught.value)
        assert service.calls == []      # never dispatched
    finally:
        client.close()


def test_a_replayed_envelope_is_refused_at_the_seam(seam):
    bridge, sock, service, _guard_obj = seam
    sealed = deploy.seal(_envelope("db"), DB_SECRET)
    client = bridge._Client(sock, correlation=sealed)
    try:
        assert client.call("Cache", "get", ["a"]) == "v:a"
        with pytest.raises(RuntimeError) as caught:
            client.call("Cache", "get", ["a"])
        assert deploy.REJECT_DUPLICATE in str(caught.value)
        assert service.calls == ["a"]   # the replay never reached the service
    finally:
        client.close()


def test_a_seam_with_no_guard_is_byte_identical_to_the_pre_118_wire(sockdir):
    """Back-compat: with no correlation configured the request line and the
    dispatch are exactly what they were."""
    bridge = _bridge()
    service = _Counter()
    running = _Seam(bridge, str(sockdir / "plain.sock"), service)
    client = bridge._Client(running.sock)
    try:
        assert client.call("Cache", "get", ["z"]) == "v:z"
    finally:
        client.close()
        running.stop()


# ---------------------------------------------------------------------------
# 2b. the ordinary `revl run --placement` path wires a LIVE guard (roadmap
#     421 F8: the guard above was tested but never wired, since bridge.serve()
#     ran with no `correlation=` and every call site with one was hand-built
#     in THIS file). This reproduces the gap the hand-built `seam` fixture
#     above could never catch: it drives a real two-process placement through
#     `revl.placement.run_placement` exactly as the CLI would, then attacks
#     the socket it published from OUTSIDE that placement, with no help from
#     `_process_runner.py` or `placement.py` at all.
# ---------------------------------------------------------------------------

_GUARD_APP = """\
service Cache { fn get(key: Str) -> Str }

component MemCache provides cache: Cache {
  provide cache { fn get(key) = "v:" + key }
}

component Consumer requires cache: Cache {}
"""

_GUARD_PLACEMENT = """\
[processes.provider]
components = ["MemCache"]

[processes.consumer]
components = ["Consumer"]
probe = ["cache.get('alice')"]
"""


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_the_ordinary_run_placement_path_leaves_a_live_correlation_guard(
        tmp_path, monkeypatch, capfd):
    """Boot a real two-process composition through the SAME `run_placement`
    the `revl run --placement` CLI calls; nothing in this test constructs a
    `CorrelationGuard`, a `Correlation`, or a `bridge.serve(correlation=...)`
    call by hand. Then dial the provider's published socket directly, as a
    stranger who never received the shared secret would, and prove the
    dispatch is refused rather than answered: the exact shape roadmap 421 F8
    found missing (`_process_runner.py` called `bridge.serve` with no
    `correlation=` at all, so any caller reached the service)."""
    if not _placement._cordis_py_installed():
        pytest.skip("cordis-py runtime not installed (sh backends/python/setup.sh)")

    app = _write(tmp_path, "app.rvl", _GUARD_APP)
    toml = _write(tmp_path, "app.toml", _GUARD_PLACEMENT)

    # Drive the swap REPL's `input()` from a queue instead of a real tty, so
    # the placement stays up (real subprocesses, real sockets) until this test
    # is done with it. `run_placement(once=True)` tears everything down the
    # instant boot finishes, before an outside dialer could ever connect.
    commands: queue.Queue = queue.Queue()

    def fake_input(prompt=""):
        item = commands.get()
        if item is None:
            raise EOFError
        return item

    monkeypatch.setattr(builtins, "input", fake_input)

    result: dict = {}

    def run():
        result["rc"] = _placement.run_placement([app], toml, once=False)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        # Poll the conductor's own interleaved log (real subprocess stdout,
        # pumped through by placement.py) for the provider's serve line. It
        # only prints "(correlation-guarded)" once `bridge.serve` was actually
        # called with a guard, which is the fact under test.
        deadline = time.time() + 30
        out = ""
        sock_path = None
        while time.time() < deadline:
            out += capfd.readouterr().out
            m = re.search(r"\[provider] serve\s*\|.*-> (\S+) \(correlation-guarded\)", out)
            if m:
                sock_path = m.group(1)
                break
            if not thread.is_alive():
                break
            time.sleep(0.1)
        assert sock_path, f"provider never reported a correlation-guarded seam:\n{out}"

        # The legitimate consumer, wired by placement.py/_process_runner.py
        # with the real secret, still gets through: its probe result is in
        # the log the moment it ran.
        while time.time() < deadline and "cache.get" not in out:
            out += capfd.readouterr().out
            time.sleep(0.1)
        assert "'v:alice'" in out or "v:alice" in out, out

        # The attack: a bare stranger dials the SAME socket with the exact
        # pre-118 wire shape (no `correlation` member at all) and is refused,
        # never dispatched, proving the guard installed by the ordinary run
        # path is live, not merely constructed.
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.settimeout(10)
        raw.connect(sock_path)
        raw.sendall((json.dumps({"key": "Cache", "method": "get", "args": ["alice"]})
                     + "\n").encode())
        reply = json.loads(raw.makefile("rb").readline())
        raw.close()
        assert reply["ok"] is False, reply
        assert reply.get("correlation_refused") == deploy.REJECT_MALFORMED, reply
    finally:
        commands.put(":q")
        thread.join(timeout=30)
        assert not thread.is_alive(), "run_placement did not tear down"
    assert result.get("rc") == 0


# ---------------------------------------------------------------------------
# 3. the coordinated cross-process commit/abort protocol
# ---------------------------------------------------------------------------


def _spawn(tmp_path, identity, effects, **extra):
    spec_path = tmp_path / f"{identity}.spec.json"
    spec = {"identity": identity,
            "world": str(tmp_path / f"{identity}.world.json"),
            "wal": str(tmp_path / f"{identity}.wal"),
            "effects": effects, **extra}
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen(
        [sys.executable, "-u", "-m", "revl._deploy_participant", str(spec_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env,
        cwd=str(ROOT))
    return deploy.ProcessParticipant(identity=identity, process=process), spec


def _world(spec):
    path = Path(spec["world"])
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _wal(spec):
    path = Path(spec["wal"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


@pytest.fixture
def two_participants(tmp_path):
    spawned = []

    def make(*args, **kwargs):
        participant, spec = _spawn(tmp_path, *args, **kwargs)
        spawned.append(participant)
        return participant, spec

    try:
        yield make
    finally:
        for participant in spawned:
            participant.stop()


def test_partial_failure_across_processes_rolls_back_with_no_residue(
        two_participants, tmp_path):
    """The core §3 property. `db` commits; `edge` fails at COMMIT; the
    conductor drives ABORT and `db` unwinds ITS OWN slice, in ITS OWN process."""
    db, db_spec = two_participants(
        "db", [{"name": "row1", "reversible": True},
               {"name": "row2", "reversible": True}])
    edge, edge_spec = two_participants(
        "edge", [{"name": "cache1", "reversible": True}], failAt=0)

    decision = str(tmp_path / "decision.jsonl")
    report = deploy.run_deploy([db, edge], approval_path=decision)

    assert report["protocol"] == deploy.PROTOCOL
    assert report["verdict"] == deploy.DEPLOY_ABORTED_CLEAN, report
    assert report["failedAt"] == "edge"
    # the abort went out in REVERSE commit order — the cross-process LIFO is an
    # ordering over messages, not a replay of inverses the conductor holds
    assert [row["identity"] for row in report["commitLedger"]] == ["db"]
    assert report["abortOrder"] == ["db"]
    assert report["participants"]["db"]["outcome"] == deploy.ROLLED_BACK_CLEAN
    # edge's local apply failed, so edge already unwound itself and never
    # counted as committed
    assert report["participants"]["edge"]["outcome"] == deploy.NEVER_COMMITTED

    # no residue anywhere in the boundary state either process owns
    assert _world(db_spec) == {}
    assert _world(edge_spec) == {}
    # and the durable abort decision was recorded BEFORE the abort went out
    kinds = [json.loads(line)["record"]
             for line in Path(decision).read_text(encoding="utf-8").splitlines()]
    assert kinds == [deploy.FEDERATION_ABORTED]


def test_the_unwind_runs_in_the_participants_process_not_the_conductors(
        two_participants, tmp_path):
    """The proof that this is a coordinated protocol, not a lift of `apply.py`'s
    in-process theorem: every `undo` record in db's WAL was written by db's OWN
    pid, which is not this process."""
    db, db_spec = two_participants("db", [{"name": "row1", "reversible": True}])
    edge, _edge_spec = two_participants(
        "edge", [{"name": "c", "reversible": True}], failAt=0)

    report = deploy.run_deploy([db, edge])
    assert report["verdict"] == deploy.DEPLOY_ABORTED_CLEAN

    undos = [r for r in _wal(db_spec) if r["record"] == "undo"]
    assert undos, "db must have unwound its own slice"
    pids = {r["pid"] for r in undos}
    assert pids == {db.process.pid}
    assert os.getpid() not in pids
    # the conductor recorded WHO unwound, and it was the participant
    assert report["participants"]["db"]["unwoundBy"] == db.process.pid


def test_an_unreachable_participant_is_unresolved_never_rolled_back(
        two_participants, tmp_path):
    """A participant the conductor cannot reach for ABORT is named
    `unresolved`, with the aggregate downgraded to `aborted-with-residue`. Its
    inverses are in its own process; claiming it rolled back would be a lie."""
    db, db_spec = two_participants(
        "db", [{"name": "row1", "reversible": True}], dieAtAbort=True)
    edge, _edge_spec = two_participants(
        "edge", [{"name": "c", "reversible": True}], failAt=0)

    report = deploy.run_deploy([db, edge])
    assert report["verdict"] == deploy.DEPLOY_ABORTED_WITH_RESIDUE, report
    assert report["participants"]["db"]["outcome"] == deploy.UNRESOLVED
    assert "recover" in report["participants"]["db"]["settleWith"]
    assert report["residue"]["clean"] is False
    # the boundary state it committed really IS still out there — the report is
    # honest about that rather than claiming a rollback
    assert _world(db_spec) == {"db:row1": True}


def test_an_irreversible_crossing_rolls_back_with_reported_residue(
        two_participants):
    db, db_spec = two_participants(
        "db", [{"name": "sent-mail", "reversible": False}])
    edge, _edge = two_participants(
        "edge", [{"name": "c", "reversible": True}], failAt=0)
    report = deploy.run_deploy([db, edge])
    assert report["verdict"] == deploy.DEPLOY_ABORTED_WITH_RESIDUE
    assert report["participants"]["db"]["outcome"] == deploy.ROLLED_BACK_WITH_RESIDUE
    assert "db:sent-mail" in report["participants"]["db"]["residue"]["outstanding"]


def test_a_prepare_refusal_is_not_a_rollback(two_participants, tmp_path):
    db, db_spec = two_participants("db", [{"name": "row1", "reversible": True}])
    edge, edge_spec = two_participants(
        "edge", [{"name": "c", "reversible": True}], refusePrepare="policy says no")
    decision = str(tmp_path / "decision.jsonl")
    report = deploy.run_deploy([db, edge], approval_path=decision)
    assert report["verdict"] == deploy.DEPLOY_REFUSED
    assert report["phase"] == "prepare"
    assert report["refusedBy"] == "edge"
    assert report["residue"]["clean"] is True
    assert _world(db_spec) == {} and _world(edge_spec) == {}
    assert not os.path.exists(decision)   # nothing was decided, nothing recorded


def test_a_clean_deploy_records_the_durable_commit_approval(
        two_participants, tmp_path):
    db, db_spec = two_participants("db", [{"name": "row1", "reversible": True}])
    edge, edge_spec = two_participants("edge", [{"name": "c", "reversible": True}])
    decision = str(tmp_path / "decision.jsonl")
    report = deploy.run_deploy([db, edge], approval_path=decision,
                              federation_id="fed-1", generation=3)
    assert report["verdict"] == deploy.DEPLOY_APPLIED, report
    assert _world(db_spec) == {"db:row1": True}
    assert _world(edge_spec) == {"edge:c": True}
    kind, record = deploy.read_decision(decision)
    assert kind == deploy.FEDERATION_APPROVED
    assert record["participants"] == ["db", "edge"]
    assert record["generation"] == 3


# ---------------------------------------------------------------------------
# 4. all-or-nothing federation update across co-located compositions
# ---------------------------------------------------------------------------

IRREVERSIBLE = """\
extern emission fn sendEmail(to: Str) = @py { pass }

component Notifier {
  emit sendEmail("a@b")
}
"""

DEFERRABLE = """\
extern emission deferred fn charge(id: Str) = @py { pass }

component Billing {
  emit charge("c1")
}
"""


# `host_put` is `deferred` so these two stay about the CONTRACT: since item
# 419b `reached_emissions` sees a tail-position `fn put(..) = emit host_put(..)`
# (which lowers to a `return` step, dropping the `emit` marker), so a bare
# emission extern here would make both versions inadmissible for an unrelated
# (and correct) reason and the contract assertion below would prove nothing.
PROVIDER_V1 = """\
service Store { emission[host_put] fn put(key: Str, value: Str) }

extern emission deferred fn host_put(line: Str) = @py { pass }

component Db provides store: Store {
  provide store {
    fn put(key, value) = emit host_put(value)
  }
}
"""

PROVIDER_V2 = """\
service Store { emission[host_put] fn erase(key: Str) }

extern emission deferred fn host_put(line: Str) = @py { pass }

component Db provides store: Store {
  provide store {
    fn erase(key) = emit host_put(key)
  }
}
"""

CONSUMER = """\
service Store { emission[host_put] fn put(key: Str, value: Str) }

component App requires store: Store {
  emit store.put("a", "b")
}
"""


def _ir(source, tmp_path, name):
    from revl.compiler import compile_source

    return compile_source(source, str(tmp_path / name))


def test_a_plan_that_must_cross_a_non_deferrable_effect_is_refused(tmp_path):
    """The second CRITICAL's refusal: PREPARE cannot HOLD a class-(c) crossing,
    so admitting the plan would let a late partition strand this composition
    with residue while its peers revert. Refused, not degraded."""
    plans = {"notifier": _ir(IRREVERSIBLE, tmp_path, "n.rvl")}
    verdict = deploy.federation_admission(plans)
    assert verdict["admitted"] is False
    (refusal,) = verdict["refusals"]
    assert refusal["kind"] == deploy.REFUSE_IRREVERSIBLE
    assert refusal["extern"] == "sendEmail"
    assert "deferred" in refusal["reason"]


def test_a_deferrable_crossing_is_admitted_and_held(tmp_path):
    plans = {"billing": _ir(DEFERRABLE, tmp_path, "b.rvl")}
    verdict = deploy.federation_admission(plans)
    assert verdict["admitted"] is True
    assert verdict["deferred"] == [
        {"composition": "billing", "component": "Billing", "extern": "charge",
         "deferrable": True, "idempotency_key": None, "via": None}]


def test_a_federation_refuses_as_one_unit_when_any_member_is_inadmissible(tmp_path):
    plans = {"billing": _ir(DEFERRABLE, tmp_path, "b.rvl"),
             "notifier": _ir(IRREVERSIBLE, tmp_path, "n.rvl")}
    verdict = deploy.federation_admission(plans)
    assert verdict["admitted"] is False
    assert {r["composition"] for r in verdict["refusals"]} == {"notifier"}


def test_a_contract_break_refuses_the_whole_federation_update(tmp_path):
    """Reuses `federation.check` whole — the §5 drift predicate, not a copy."""
    from revl.compiler import compile_source
    from revl.federation import consumer_surface

    provider_v1 = compile_source(PROVIDER_V1, str(tmp_path / "p1.rvl"))
    consumer = compile_source(CONSUMER, str(tmp_path / "c.rvl"))
    surface = consumer_surface(consumer, consumer="app")
    assert surface["requires"], "the consumer must pin an external service"

    # v2 drops the pinned operation: a MAJOR break
    provider_v2 = compile_source(PROVIDER_V2, str(tmp_path / "p2.rvl"))
    ok = deploy.federation_admission({"db": provider_v1}, contracts=[(surface, "db")])
    assert ok["admitted"] is True
    broken = deploy.federation_admission({"db": provider_v2},
                                         contracts=[(surface, "db")])
    assert broken["admitted"] is False
    assert broken["refusals"][0]["kind"] == deploy.REFUSE_CONTRACT


def test_a_contract_naming_a_provider_outside_the_update_is_refused(tmp_path):
    """428 F13. It used to be SKIPPED, so a typo'd or dropped provider left the
    pin checked against nothing and the federation admitted, in a function
    whose whole posture is refuse-as-one-unit."""
    from revl.compiler import compile_source
    from revl.federation import consumer_surface

    provider_v1 = compile_source(PROVIDER_V1, str(tmp_path / "p1.rvl"))
    surface = consumer_surface(
        compile_source(CONSUMER, str(tmp_path / "c.rvl")), consumer="app")

    verdict = deploy.federation_admission({"db": provider_v1},
                                          contracts=[(surface, "typo-db")])
    assert verdict["admitted"] is False
    refusal = verdict["refusals"][0]
    assert refusal["kind"] == deploy.REFUSE_UNKNOWN_PROVIDER
    assert refusal["composition"] == "typo-db"
    assert "carries no plan for it" in refusal["reason"]
    # the refusal names what IS being updated, so the author can see the typo
    assert "updating: db" in refusal["reason"]


# --- roadmap 419b: the two shapes `reached_emissions` used to walk past ------

_MEDIATED = """\
service Store { emission[host_put] fn put(key: Str, value: Str) }

component App requires store: Store {
  emit store.put("a", "b")
}
"""

_TAIL_EMIT = """\
service Store { emission[host_put] fn put(key: Str, value: Str) }

extern emission fn host_put(line: Str) = @py { pass }

component Db provides store: Store {
  provide store {
    fn put(key, value) = emit host_put(value)
  }
}
"""

_LOCAL_DEFERRED = """\
service Store { emission[host_put] fn put(key: Str, value: Str) }

extern emission deferred fn host_put(line: Str) = @py { pass }

component Db provides store: Store {
  provide store {
    fn put(key, value) = emit host_put(value)
  }
}

component App requires store: Store {
  emit store.put("a", "b")
}
"""


def test_a_service_mediated_emission_is_a_counted_crossing(tmp_path):
    """419b: the consumer writes `emit store.put(..)` and never names the extern.
    The key resolves to no provider in this composition, so the crossing fires
    against an external provider during this composition's local commit, and
    deferral is not spellable on a service method, so PREPARE cannot hold it."""
    ir = _ir(_MEDIATED, tmp_path, "m.rvl")
    assert deploy.reached_emissions(ir) == [
        {"component": "App", "extern": "host_put", "deferrable": False,
         "idempotency_key": None, "via": "store.put"}]
    verdict = deploy.federation_admission({"app": ir})
    assert verdict["admitted"] is False
    (refusal,) = verdict["refusals"]
    assert refusal["kind"] == deploy.REFUSE_IRREVERSIBLE
    # the refusal names what the AUTHOR wrote, not only the far side's extern
    assert "store.put" in refusal["reason"]


def test_a_tail_position_provide_method_emit_is_a_counted_crossing(tmp_path):
    """419b: `fn put(k, v) = emit host_put(v)` lowers to a `return` step (the
    `emit` marker does not survive), so the old step-keyed walk missed it and
    admitted a provider that necessarily crosses an irreversible emission."""
    ir = _ir(_TAIL_EMIT, tmp_path, "t.rvl")
    assert deploy.reached_emissions(ir) == [
        {"component": "Db", "extern": "host_put", "deferrable": False,
         "idempotency_key": None, "via": None}]
    assert deploy.federation_admission({"db": ir})["admitted"] is False


def test_a_locally_provided_deferred_crossing_is_counted_once_and_held(tmp_path):
    """The other half of 419b, and the reason the mediated row is skipped when
    the key resolves locally: counting the consumer's call as well would
    downgrade a genuinely `deferred` extern to class (c) and refuse a plan
    PREPARE can hold perfectly well."""
    ir = _ir(_LOCAL_DEFERRED, tmp_path, "l.rvl")
    assert deploy.reached_emissions(ir) == [
        {"component": "Db", "extern": "host_put", "deferrable": True,
         "idempotency_key": None, "via": None}]
    verdict = deploy.federation_admission({"shop": ir})
    assert verdict["admitted"] is True
    assert [row["component"] for row in verdict["deferred"]] == ["Db"]


# --- a stranded participant settles by the durable record, never a guess ----


def _stranded_wal(path, *, complete=False):
    """A participant WAL in `recovery.py`'s own schema, stranded mid-update: one
    transactional inverse and one held (class-(b)) deferred emission."""
    records = [
        {"record": "header", "walVersion": 1, "generation": 0,
         "guarantee": "test"},
        {"record": "discharge-descriptor", "seq": 1, "entry": "transactional",
         "call": {"receiver": "db", "method": "removeRow", "args": ["r1"]},
         "origin": "Billing"},
        {"record": "deferred-emission", "seq": 2,
         "call": {"receiver": "mail", "method": "send", "args": ["a@b"]}},
    ]
    if complete:
        records.append({"record": "activation-complete", "components": ["Billing"]})
    Path(path).write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records),
        encoding="utf-8")


def test_a_stranded_participant_rolls_forward_when_the_record_is_present(tmp_path):
    wal = str(tmp_path / "p.wal")
    decision = str(tmp_path / "decision.jsonl")
    _stranded_wal(wal)
    deploy.write_commit_approval(decision, federation_id="fed", generation=1,
                                 participants=["billing"])
    verdict = deploy.settle_stranded(wal, decision)
    assert verdict["verdict"] == "rolled-forward"
    assert verdict["committed"] is True
    assert verdict["federationDecision"] == deploy.FEDERATION_APPROVED
    # recovery's own rule ran: it replayed no inverse and rolled the missing
    # discharge forward instead
    assert verdict["rolledForwardDischarge"] == [1]


def test_a_stranded_participant_rolls_back_when_the_record_is_absent(tmp_path):
    wal = str(tmp_path / "p.wal")
    decision = str(tmp_path / "missing.jsonl")
    _stranded_wal(wal)
    verdict = deploy.settle_stranded(wal, decision)
    assert verdict["verdict"] == "rolled-back"
    assert verdict["federationDecision"] == "none"


def test_a_stranded_participant_rolls_back_on_an_explicit_abort_record(tmp_path):
    wal = str(tmp_path / "p.wal")
    decision = str(tmp_path / "decision.jsonl")
    _stranded_wal(wal)
    deploy.write_abort_decision(decision, federation_id="fed", generation=1,
                                reason="peer failed", failed="edge")
    verdict = deploy.settle_stranded(wal, decision)
    assert verdict["verdict"] == "rolled-back"
    assert verdict["federationDecision"] == deploy.FEDERATION_ABORTED


def test_an_unreadable_decision_record_fails_closed(tmp_path):
    """No guess. Guessing between roll-forward and roll-back IS the split-brain
    the durable record exists to prevent."""
    wal = str(tmp_path / "p.wal")
    decision = tmp_path / "decision.jsonl"
    _stranded_wal(wal)
    decision.write_text("{not json at all\n", encoding="utf-8")
    verdict = deploy.settle_stranded(wal, str(decision))
    assert verdict["verdict"] == deploy.UNRESOLVED
    assert verdict["residue"]["clean"] is False
    assert "will NOT guess" in verdict["decision"]
    # nothing was settled: the WAL is untouched
    assert not any(r.get("record") == "commit-approved"
                   for r in [json.loads(line)
                             for line in Path(wal).read_text(encoding="utf-8").splitlines()
                             if line.strip()])


# --- R-C8: a REAL participant WAL is readable by recover/settle_stranded ----
#
# The tests above hand-write a WAL in `recovery.py`'s own schema. R-C8 (issue
# #538) is that the WAL a real `_deploy_participant` process ACTUALLY writes
# used an `effect`/`inverse` schema recover could not read: recover's
# `_referent_key` calls `World.key(inverse["op"])`, which did `op.get(...)` and
# raised `AttributeError` on the old `{"op": "remove", ...}` string form — so a
# genuinely stranded participant's own WAL tracebacked the very tool meant to
# settle it. These tests drive the real participant to produce the WAL, then
# settle it, proving the schemas now agree.


def _stranded_participant_wal(tmp_path, identity="db",
                              effects=("row1", "row2")):
    """Drive a real `_deploy_participant._Participant` to apply its effects and
    then STOP before `activation-complete` — the on-disk shape a crash mid
    activation leaves. Returns its WAL path."""
    from revl import _deploy_participant as dp  # noqa: PLC0415

    wal = tmp_path / f"{identity}.wal"
    part = dp._Participant({
        "identity": identity,
        "world": str(tmp_path / f"{identity}.world.json"),
        "wal": str(wal),
        "effects": [{"name": name, "reversible": True} for name in effects],
    })
    for effect in part.effects:
        part._apply_one(effect)   # writes real `effect` records, no completion
    return str(wal)


def test_a_real_participant_wal_rolls_back_cleanly(tmp_path):
    """No durable decision -> roll back. recover must READ the real participant
    WAL (not traceback) and re-issue each reconstructible boundary inverse LIFO,
    leaving no residue."""
    wal = _stranded_participant_wal(tmp_path)
    missing = str(tmp_path / "no-decision.jsonl")

    verdict = deploy.settle_stranded(wal, missing)

    assert verdict["verdict"] == "rolled-back"
    assert verdict["federationDecision"] == "none"
    # both reconstructible inverses actually replayed, and nothing is left out
    assert len(verdict["ran"]) == 2
    assert verdict["residue"]["clean"] is True
    assert verdict["residue"]["worldRemaining"] == []


def test_a_real_participant_wal_reads_through_recover_directly(tmp_path):
    """The same WAL read straight through `recovery.recover` — the readability
    itself is the regression guard: the old schema raised `AttributeError`
    here."""
    from revl import recovery  # noqa: PLC0415

    wal = _stranded_participant_wal(tmp_path, identity="edge", effects=("c1",))
    report = recovery.recover(wal)
    assert report["verdict"] == "rolled-back"
    assert len(report["ran"]) == 1
    assert report["residue"]["clean"] is True


def test_a_real_participant_wal_rolls_forward_when_committed(tmp_path):
    """A durable `commit-approved` decision -> roll forward. settle_stranded
    hands recover the marker and recover rolls the missing discharge forward,
    replaying NO inverse — over the real participant WAL."""
    wal = _stranded_participant_wal(tmp_path)
    decision = str(tmp_path / "decision.jsonl")
    deploy.write_commit_approval(decision, federation_id="fed", generation=1,
                                 participants=["db"])

    verdict = deploy.settle_stranded(wal, decision)

    assert verdict["verdict"] == "rolled-forward"
    assert verdict["federationDecision"] == deploy.FEDERATION_APPROVED


# --- R-C9: the write-behind world file is replaced ATOMICALLY ---------------


def test_participant_world_write_is_atomic(tmp_path, monkeypatch):
    """R-C9 (issue #538): the write-behind boundary-state file is replaced
    atomically. A plain truncate-then-write left a half-written or empty world
    file when the process died mid-write; the recovery path then read that torn
    file as lost state. A failed write must leave the PREVIOUS contents intact
    and no temp residue behind."""
    from revl import _deploy_participant as dp  # noqa: PLC0415

    world = tmp_path / "db.world.json"
    part = dp._Participant({"identity": "db", "world": str(world),
                            "wal": str(tmp_path / "db.wal"), "effects": []})

    # a first, whole write
    part._write_world({"db:row1": True})
    assert json.loads(world.read_text(encoding="utf-8")) == {"db:row1": True}
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]

    # now make the atomic swap itself fail, mid-write
    def _boom(src, dst):
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(dp.os, "replace", _boom)
    with pytest.raises(OSError):
        part._write_world({"db:row2": True})

    # the previous generation's world survived whole — not truncated, not empty
    assert json.loads(world.read_text(encoding="utf-8")) == {"db:row1": True}
    # and the failed write left no sibling temp file behind
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]

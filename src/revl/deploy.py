"""`revl deploy` — attested admission, correlated seams, coordinated rollback
(roadmap item 118, Slice 1: single host / local multiprocess).

The design (docs/design/118-revl-deploy.md) leads with a finding that reshapes
everything built here: **the LIFO rollback theorem `apply` proves is an
IN-PROCESS property and it does not cross a seam.** A peer process's inverses
live in that process's memory and its own durable WAL; the conducting process
holds neither. So nothing in this module lifts `apply.py`'s theorem across a
boundary. What crosses is a *protocol*, and the honest verdict vocabulary that
comes with it.

Slice 1 is the cut that is landable on today's tree: every seam is a LOCAL
process seam (a UDS bridge, item 56's transport reused unchanged), so the
control plane a cross-machine deploy would need — SSH/container launch, a
replicated WAL, a quorum coordinator — is deliberately absent (Slice 2+).
What Slice 1 does build, in the four pieces the design names:

1. **the attestation chain, verified at admission** (§2). `chain_bindings`
   folds the per-facet sha256 of the artifact bytes, the capability policy, the
   component lock, the gauntlet evidence and the per-backend conformance cert
   into item 127's signed `evidence_bindings` (item 290's hook), so one
   signature commits `source -> IR -> artifact -> policy -> evidence`.
   :func:`admit` verifies it on the RECEIVING side, against a local
   :class:`TrustStore`.

   The binding rule that makes this worth anything (Addendum 3a): the receiver
   **re-hashes the IR and the artifact bytes it will actually execute** and
   never trusts a self-declared `backend`/`artifact_hash` in the attestation.
   The signed chain is checked against the bytes in hand, not the bytes the
   sender claims about itself. :data:`SELF_DECLARED_IGNORED` names the members
   admission structurally refuses to read.

2. **effect-correlation identity on every seam crossing** (§1.4). A
   :class:`Correlation` envelope — `{composition_id, generation, realm,
   effect_id, idempotency_key, parent_effect}` plus the peer's identity — rides
   the existing JSON-line request. The binding rule (Addendum 3b): the envelope
   is **authenticated against the peer identity**, and duplicate detection is
   scoped on `(peer_identity, composition_id, generation, idempotency_key)`, so
   a peer cannot forge another peer's identity or replay its envelope.
   :class:`CorrelationGuard` is what `bridge.serve` runs before dispatch.

3. **coordinated cross-process rollback** (§3). :func:`run_deploy` drives a
   two-phase PREPARE/COMMIT over :class:`Participant`s that are *other
   processes*. The coordinator holds an ordered **commit ledger** — who
   committed, and when — and NOTHING ELSE. It never holds an inverse and never
   runs one. On a COMMIT failure it drives ABORT in reverse ledger order and
   each participant runs its OWN local LIFO unwind, in its own process, against
   its own accumulator and WAL. A participant it cannot reach is reported
   `unresolved(...)`, never `rolled-back`. That distinction — a coordinated
   protocol over per-process-atomic units versus one lifted theorem — is the
   whole content of the first CRITICAL.

4. **all-or-nothing federation update across co-located compositions**
   (Addendum 2, the second CRITICAL). No composition may cross an irreversible
   effect until a durable `federation-commit-approved` record exists. PREPARE
   holds every irreversible crossing as an item-245 class-(b) deferred
   emission, so a plan that NECESSARILY crosses a non-deferrable irreversible
   effect is REFUSED admission (:func:`federation_admission`) rather than
   silently degrading atomicity. A stranded participant applies
   `recovery.py`'s existing rule VERBATIM — record present, roll forward;
   absent, roll back — and fails closed on a guess (:func:`settle_stranded`).

Deliberately NOT here, and named so the boundary is explicit: cross-machine
orchestration (SSH/container launch), a replicated WAL, a quorum coordinator,
and the Ed25519 upgrade to `attest.py`. The last one is a HARD prerequisite for
a cross-trust-domain deploy (§2.4/§5-A1): a symmetric HMAC verifier is also a
forger, so "signer untrusted" would be a fiction. Slice 1 is single-host, where
signer and host are the same trust domain, so HMAC is honest here — and
:func:`admit` REFUSES outright when a :class:`TrustStore` declares itself
`cross_domain` under a symmetric algorithm, rather than pretending.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from . import attest
from .errors import RevlError

# ---------------------------------------------------------------------------
# §2. the attestation chain
# ---------------------------------------------------------------------------

#: Bundle-relative locations of every facet the chain binds (item 305's layout).
IR_DOCUMENT = "ir/ir.json"
EMITTED_ROOT = "emitted"
#: the bundled `.rvl` sources and the bundle's own manifest — the two the signer
#: re-runs the frontend over, so an attestation is signed over a composition
#: this toolchain admitted rather than over an IR document someone handed it.
SOURCE_ROOT = "source"
RUNTIME_MANIFEST = "runtime-manifest.json"
POLICY_NAME = "policy.json"
LOCK_NAME = "components.lock"
GAUNTLET_NAME = "gauntlet.json"
ATTESTATION_NAME = "attestation.json"
#: item 306's per-backend conformance certs, staged as `conformance/<backend>.json`.
CONFORMANCE_ROOT = "conformance"

#: The facet keys inside `evidence_bindings`. `artifact/<backend>` and
#: `conformance/<backend>` are per-backend; the rest are whole-bundle.
FACET_POLICY = "policy"
FACET_LOCK = "lock"
FACET_GAUNTLET = "gauntlet"


def artifact_facet(backend: str) -> str:
    """The `evidence_bindings` key binding one backend's emitted artifact."""
    return f"artifact/{backend}"


def conformance_facet(backend: str) -> str:
    """The `evidence_bindings` key binding one backend's item-306 cert."""
    return f"conformance/{backend}"


#: Members a sender may *write into* an attestation to describe the artifact it
#: claims to have emitted. Admission NEVER reads them: the chain is checked
#: against the bytes in hand (Addendum 3a). Named as data so the refusal to
#: trust them is a stated property and a test can pin it.
#:
#: `sign_alg` is deliberately NOT on this list and never was — but it also is no
#: longer self-declared in any meaningful sense: `attest.verify_attestation`
#: refuses any value but `attest.SIGN_ALG`, and admission's cross-domain refusal
#: reads the algorithm off THIS build rather than off the record.
SELF_DECLARED_IGNORED = ("backend", "artifact_hash", "artifact_sha256",
                         "emitted_hash", "ir_hash")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_digest(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def artifact_digest(backend_dir: Path | str) -> str:
    """The digest of one backend's emitted artifact — a *tree* digest, since a
    backend may emit several files (the wasm emitter emits one `.wat` per
    module).

    Canonical by construction: the relative path of each file and its bytes are
    folded in sorted-path order with explicit length framing, so no rename or
    split of the emitted set can collide with another. This is the value the
    signer binds and the value the receiver RE-COMPUTES from the bytes it is
    about to execute.
    """
    root = Path(backend_dir)
    digest = hashlib.sha256()
    files = sorted(p for p in root.rglob("*") if p.is_file())
    for path in files:
        rel = path.relative_to(root).as_posix().encode("utf-8")
        data = path.read_bytes()
        digest.update(len(rel).to_bytes(8, "big"))
        digest.update(rel)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def chain_bindings(bundle_dir: Path | str, *,
                   backends: Optional[Iterable[str]] = None) -> dict:
    """Collect every link of the chain as a per-facet sha256, ready to fold into
    item 127's signed `evidence_bindings`.

    Nothing here is re-derived: `bundle.py` already wrote `components.lock`,
    `policy.json`, `emitted/<backend>/...` and `gauntlet.json`, and
    `conformance_cert.py` already wrote the per-backend cert. This hashes those
    bytes so ONE signature commits the whole chain (§2.1). A facet the bundle
    does not carry is simply absent from the bindings — and an absent binding is
    what makes :func:`admit` refuse with `backend` when the host's own backend
    has no `artifact/<backend>` link.
    """
    root = Path(bundle_dir)
    bindings: dict[str, str] = {}
    for facet, name in ((FACET_POLICY, POLICY_NAME),
                        (FACET_LOCK, LOCK_NAME),
                        (FACET_GAUNTLET, GAUNTLET_NAME)):
        path = root / name
        if path.is_file():
            bindings[facet] = _file_digest(path)
    emitted = root / EMITTED_ROOT
    names = sorted(backends) if backends is not None else sorted(
        p.name for p in emitted.iterdir() if p.is_dir()) if emitted.is_dir() else []
    for backend in names:
        backend_dir = emitted / backend
        if backend_dir.is_dir():
            bindings[artifact_facet(backend)] = artifact_digest(backend_dir)
        cert = root / CONFORMANCE_ROOT / f"{backend}.json"
        if cert.is_file():
            bindings[conformance_facet(backend)] = _file_digest(cert)
    return bindings


def staged_ir(bundle_dir: Path | str) -> dict:
    """The IR document as it exists ON THE RECEIVER's disk.

    Read here, and only here, so every hash admission compares comes from the
    staged bytes rather than from anything the sender asserted about them.
    """
    path = Path(bundle_dir) / IR_DOCUMENT
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RevlError(str(path), 0,
                        f"cannot read the staged IR document: {error}") from error


def capability_surface(ir: Mapping) -> tuple[set[str], dict]:
    """The capability labels a composition reaches, DERIVED FROM THE IR.

    This is the same derivation `bundle.build_bundle` runs to *write*
    `policy.json` (`_policy_of(_audit_document(ir))`, itself G4's own boundary
    analysis over the IR), so `policy.json` is a projection of this value and
    never the other way round.

    Admission needs the value, not the projection. `ir/ir.json` is the only
    capability authority a receiver has that the deploying side cannot move:
    admission has already re-derived its `composition_hash` from the staged
    bytes and checked it against the SIGNED one, so an IR whose boundary
    analysis reaches fewer capabilities is a different composition and refuses
    at :data:`LINK_COMPOSITION` before the ceiling is ever consulted. It is also
    the document every emitter emits from, so the capability set it yields is
    the set the artifact in hand can actually reach.

    Returns `(capability labels, the full derived policy surface)`. Raises
    :class:`RevlError` when the surface cannot be derived: the ceiling has no
    honest answer then, and its caller refuses.
    """
    import copy  # noqa: PLC0415 (lazy, with the two below)

    from .bundle import _policy_of          # noqa: PLC0415 (avoids a cycle)
    from .registry import _audit_document   # noqa: PLC0415

    try:
        surface = _policy_of(_audit_document(copy.deepcopy(dict(ir))))
    except Exception as error:              # noqa: BLE001 (any failure refuses)
        raise RevlError(IR_DOCUMENT, 0,
                        "cannot derive the capability surface from the staged "
                        f"IR: {type(error).__name__}: {error}") from error
    labels = surface.get("capabilities")
    if not isinstance(labels, (list, tuple, set, frozenset)):
        raise RevlError(IR_DOCUMENT, 0,
                        "the capability surface derived from the staged IR has "
                        f"no readable `capabilities` member (got {labels!r})")
    return {str(label) for label in labels}, surface


def staged_sources(bundle_dir: Path | str) -> list[str]:
    """The bundle's `.rvl` sources, in the order the bundle recorded them.

    Order matters: `compile_files` composes the files in the order given, so the
    recompile must use the recorded order to reproduce the staged IR. Falls back
    to sorted basenames for a bundle with no readable manifest.
    """
    root = Path(bundle_dir)
    names: list = []
    manifest_path = root / RUNTIME_MANIFEST
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            manifest = {}
        names = [rec.get("name")
                 for rec in ((manifest.get("source") or {}).get("files") or [])]
    source_dir = root / SOURCE_ROOT
    if not names and source_dir.is_dir():
        names = sorted(path.name for path in source_dir.iterdir()
                       if path.suffix == ".rvl")
    return [str(source_dir / name) for name in names if name]


def gate_bundle(bundle_dir: Path | str) -> attest.GateVerdict:
    """Run the reference frontend over the bundle's OWN staged source and return
    the verdict, hashed in the bundle's normalized IR spelling.

    This is the measurement an attestation over a bundle records. It is also
    what makes `attest.make_attestation`'s hash equality meaningful here: the
    verdict's hash is the recompile's, so signing succeeds only when the staged
    `ir/ir.json` is REPRODUCED by compiling the staged source — the same
    property `revl verify` reports, enforced at signing time.
    """
    from .bundle import _canonical_ir  # noqa: PLC0415 — lazy, avoids an import cycle

    return attest.run_gate(paths=staged_sources(bundle_dir),
                           normalize=_canonical_ir)


def make_deploy_attestation(bundle_dir: Path | str, key: bytes, *,
                            backends: Optional[Iterable[str]] = None,
                            now=None, signer: str | None = None) -> dict:
    """Sign the deploy attestation for a staged bundle: item 127's attestation
    over the bundle's IR, with the whole chain folded into `evidence_bindings`.

    A thin composition on purpose — the signature primitive, the canonical IR
    hash and the bindings member are all item 127/290's, unchanged. What is NOT
    thin, and is the point: the gate verdict comes from :func:`gate_bundle`, a
    real frontend run over the staged source. A bundle whose source no longer
    admits, or whose staged IR is not what its source compiles to, is REFUSED
    signing rather than signed with a guarantee list nothing measured.
    """
    ir = staged_ir(bundle_dir)
    verdict = gate_bundle(bundle_dir)
    if not verdict.admitted and verdict.error is not None:
        raise verdict.error
    return attest.make_attestation(
        ir, key, verdict=verdict, now=now, signer=signer,
        evidence_bindings=chain_bindings(bundle_dir, backends=backends))


# ---------------------------------------------------------------------------
# §2.4. host-side admission and the trust store
# ---------------------------------------------------------------------------

#: The six named links a host refuses on (§2.2). A refusal always names one, so
#: it is a mechanical check with a located cause, never a judgement call.
LINK_SIGNER = "signer-untrusted"
LINK_SIGNATURE = "signature"
LINK_COMPOSITION = "source-or-ir-hash"
LINK_BACKEND = "backend"
LINK_ARTIFACT = "artifact-bytes"
LINK_POLICY = "policy"
LINK_CAPABILITY = "capability-ceiling"
LINK_EVIDENCE = "evidence-stale"

ACCEPT = "ACCEPT"
REFUSE = "REFUSE"

RECEIPT_KIND = "revl.deploy.receipt"
RECEIPT_VERSION = "1.0"

#: The domain-separation prefix folded into every receipt MAC, distinct from
#: `attest.SIGN_DOMAIN`. Both MACs are `hmac(key, canonical(body-minus-
#: signature))` over the same canonical spelling, so without a per-protocol tag
#: an ACCEPT receipt verified as a valid attestation and an attestation verified
#: as a valid receipt — cross-protocol confusion between two records that mean
#: entirely different things ("I admitted this" vs "this was admitted").
RECEIPT_DOMAIN = b"revl.deploy.receipt/v1\x00"


def _receipt_mac(body: Mapping, host_key: bytes) -> str:
    """The receipt MAC: domain-tagged HMAC-SHA256 over the canonical body bytes
    (`body` is the receipt with its `signature` member removed)."""
    payload = json.dumps({k: v for k, v in body.items() if k != "signature"},
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(bytes(host_key), RECEIPT_DOMAIN + payload,
                    hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class TrustStore:
    """What the RECEIVING side independently knows, and admits against.

    The whole security value of §2.4 is that this lives on the receiver: the
    deploying operator does not get to assert "trust me, it is admitted". A
    receiver holds its own verify keys, its own capability ceiling, its own
    freshness policy, and its own backend — and recomputes the chain over the
    staged bytes before anything loads.

    `cross_domain` records whether the signer is a DIFFERENT trust domain than
    this receiver. Under a symmetric signature algorithm that combination is
    refused outright (§5-A1): a verifier that holds the HMAC secret can forge an
    attestation binding arbitrary bytes, so "signer untrusted" would be a
    fiction. Slice 1 is single-host — one trust domain — so the default is
    False and HMAC is honest; the flag is the place the Ed25519 prerequisite
    lands when Slice 2 crosses a domain.

    `recheck_source` is the receiver's answer to "a signature is not a check".
    Off (the default), admission trusts the SIGNER's gate run: the attestation
    is only issuable from a real admitted verdict over this exact
    `composition_hash`, so admission inherits that transitively, and what it
    leaves open is a signer whose frontend was older, patched, or lying. On, the
    receiver re-runs its OWN frontend over the bundle's staged source and
    refuses unless that run admits AND reproduces the staged IR — the receiver
    stops taking the signer's word for the verdict as well as for the bytes.
    """

    keys: Mapping[str, bytes]
    backend: str
    revoked: frozenset = frozenset()
    capability_ceiling: Optional[frozenset] = None
    evidence_ttl_seconds: Optional[float] = None
    cross_domain: bool = False
    recheck_source: bool = False

    def key_for(self, kid: str | None) -> Optional[bytes]:
        if not isinstance(kid, str):
            return None
        if kid in self.revoked:
            return None
        return self.keys.get(kid)


def _refusal(link: str, reason: str, **extra) -> dict:
    return {"kind": RECEIPT_KIND, "version": RECEIPT_VERSION,
            "verdict": REFUSE, "link": link, "reason": reason, **extra}


def _parse_timestamp(value) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def admit(bundle_dir: Path | str, *, trust: TrustStore,
          attestation: Optional[dict] = None, now=None,
          nonce: str | None = None, host_key: Optional[bytes] = None,
          runtime_versions: Optional[Mapping[str, str]] = None) -> dict:
    """Verify the whole attestation chain against the STAGED BYTES and return a
    signed admission verdict. Loads nothing; this is PREPARE (§1.3, step 4/5).

    Every hash on the right-hand side of every comparison is recomputed here,
    from the receiver's own disk:

      * `composition_hash` is re-derived with `attest.canonical_hash` over
        `ir/ir.json` as staged — not read off the attestation's own claim about
        the source;
      * `artifact/<backend>` is re-derived with :func:`artifact_digest` over
        `emitted/<trust.backend>/...` as staged — the bytes this receiver will
        actually execute;
      * `policy` / `lock` / `gauntlet` / `conformance/<backend>` are re-derived
        from their staged files.

    The attestation contributes exactly one thing: the SIGNED left-hand side.
    Members in :data:`SELF_DECLARED_IGNORED` are never read, so an attestation
    that truthfully describes bytes other than the ones in hand still fails —
    a signature proves authorship, not that the artifact on this disk is the
    artifact that was signed. The signature and the re-hash are two independent
    gates and both must pass.

    Returns an ACCEPT receipt (binding the RE-HASHED artifact hash, the
    re-derived composition hash, the receiver's runtime versions and a fresh
    nonce) or a REFUSE receipt naming the failing chain link.
    """
    root = Path(bundle_dir)
    if attestation is None:
        path = root / ATTESTATION_NAME
        if not path.is_file():
            return _refusal(LINK_SIGNATURE,
                            f"the staged bundle carries no {ATTESTATION_NAME}: "
                            "there is no chain to verify, so it is refused "
                            "rather than admitted unattested")
        attestation = attest.load_attestation(str(path))

    # (a) signer: is this key_id one this receiver trusts, and not revoked?
    kid = attestation.get("key_id")
    # The cross-domain refusal reads NOTHING off the attestation. It used to
    # gate on `attestation["sign_alg"] == SIGN_ALG`, which made a self-declared
    # member the decider of the trust-domain question: relabel it `ed25519` and
    # the refusal evaporated, while `verify_attestation` went on MAC-ing with
    # the symmetric key regardless. The algorithm this build can verify is a
    # property of THIS code, so it is read from this code: every attestation
    # `attest.verify_attestation` accepts is HMAC (it now refuses any other
    # `sign_alg`), and a symmetric verifier is a forger, so a declared
    # cross-domain deploy is refused outright.
    if trust.cross_domain:
        return _refusal(
            LINK_SIGNER,
            "this receiver declares the signer a different trust domain, but "
            f"every attestation this build verifies is the symmetric "
            f"{attest.SIGN_ALG!r}. A verifier that holds the secret is also a "
            "forger, so `signer untrusted` would be a fiction. Refusing until "
            "the asymmetric (Ed25519) upgrade lands "
            "(docs/design/118-revl-deploy.md §2.4).")
    key = trust.key_for(kid)
    if key is None:
        known = ", ".join(sorted(trust.keys)) or "(none)"
        revoked = " (REVOKED on this receiver)" if kid in trust.revoked else ""
        return _refusal(LINK_SIGNER,
                        f"signer key_id {kid!r} is not in this receiver's trust "
                        f"store{revoked} (trusted: {known})", key_id=kid)

    # (b) signature: authentic and untampered, before any field is trusted.
    ok, reason = attest.verify_attestation(attestation, key)
    if not ok:
        return _refusal(LINK_SIGNATURE, reason, key_id=kid)

    # (c) RE-HASH the IR that is actually staged here.
    try:
        ir = staged_ir(root)
    except RevlError as error:
        return _refusal(LINK_COMPOSITION, str(error))
    recomputed_ir = attest.canonical_hash(ir)
    bound_ir = attestation.get("composition_hash")
    if not hmac.compare_digest(recomputed_ir, str(bound_ir)):
        return _refusal(
            LINK_COMPOSITION,
            "the staged IR is not the attested composition: the signature binds "
            f"{str(bound_ir)[:12]}…, the bytes on this receiver hash to "
            f"{recomputed_ir[:12]}…")

    # (c2) optionally, RE-RUN the frontend here. `admit` re-hashes the IR but a
    # hash is not a verdict: without this the receiver still takes the signer's
    # word that some checker ever admitted the composition. With it, this
    # receiver's own frontend must admit the staged source AND reproduce the
    # staged IR (docs/design/118-revl-deploy.md §2.4, TrustStore.recheck_source).
    if trust.recheck_source:
        local = gate_bundle(root)
        if not local.admitted:
            return _refusal(
                LINK_COMPOSITION,
                "this receiver re-ran its own frontend over the staged source "
                f"and it was NOT admitted: {local.reason}")
        if not hmac.compare_digest(str(local.composition_hash), recomputed_ir):
            return _refusal(
                LINK_COMPOSITION,
                "the staged source does not compile to the staged IR on this "
                f"receiver: its own frontend produced "
                f"{str(local.composition_hash)[:12]}…, the staged ir/ir.json "
                f"hashes to {recomputed_ir[:12]}…")

    bindings = attestation.get("evidence_bindings") or {}

    # (d) backend: the chain must bind an artifact for THIS receiver's backend.
    facet = artifact_facet(trust.backend)
    if facet not in bindings:
        bound = ", ".join(sorted(k for k in bindings if k.startswith("artifact/"))) or "(none)"
        return _refusal(
            LINK_BACKEND,
            f"the signed chain binds no artifact for this receiver's backend "
            f"{trust.backend!r} (bound: {bound}); it was attested for a "
            "different runtime target")

    # (e) RE-HASH the artifact bytes this receiver would execute.
    backend_dir = root / EMITTED_ROOT / trust.backend
    if not backend_dir.is_dir():
        return _refusal(LINK_ARTIFACT,
                        f"the staged bundle has no emitted/{trust.backend}/ "
                        "artifact to execute")
    recomputed_artifact = artifact_digest(backend_dir)
    if not hmac.compare_digest(recomputed_artifact, str(bindings[facet])):
        return _refusal(
            LINK_ARTIFACT,
            f"the artifact bytes staged at emitted/{trust.backend}/ are NOT the "
            f"attested ones: the signature binds {str(bindings[facet])[:12]}…, "
            f"the bytes on this receiver hash to {recomputed_artifact[:12]}…. "
            "The chain is checked against the bytes in hand, never against the "
            "sender's own claim about them.")

    # (f) every other bound facet, re-hashed from the staged file.
    for facet_name, filename in ((FACET_POLICY, POLICY_NAME),
                                 (FACET_LOCK, LOCK_NAME),
                                 (FACET_GAUNTLET, GAUNTLET_NAME)):
        if facet_name not in bindings:
            continue
        path = root / filename
        if not path.is_file():
            return _refusal(LINK_POLICY if facet_name == FACET_POLICY else facet_name,
                            f"the chain binds `{facet_name}` but the staged "
                            f"bundle has no {filename}")
        if not hmac.compare_digest(_file_digest(path), str(bindings[facet_name])):
            return _refusal(
                LINK_POLICY if facet_name == FACET_POLICY else facet_name,
                f"the staged {filename} is not the bound `{facet_name}` facet "
                "(re-hashed on this receiver, and it differs)")
    cert = root / CONFORMANCE_ROOT / f"{trust.backend}.json"
    cert_facet = conformance_facet(trust.backend)
    if cert_facet in bindings:
        if not cert.is_file():
            return _refusal(LINK_EVIDENCE,
                            f"the chain binds {cert_facet} but no conformance "
                            f"cert for {trust.backend} is staged")
        if not hmac.compare_digest(_file_digest(cert), str(bindings[cert_facet])):
            return _refusal(LINK_EVIDENCE,
                            f"the staged conformance cert for {trust.backend} is "
                            "not the bound one (re-hashed here, and it differs)")

    # (g) capability ceiling: a deploy may not widen authority (§2.2, G1/G9).
    #
    # Measured off `ir/ir.json`, the AUTHORITY, and never off `policy.json`,
    # which is a projection `bundle.build_bundle` derives from that same IR. The
    # ceiling used to read the projection, and a projection is writable by the
    # party the ceiling constrains: deleting `policy.json`, leaving it valid but
    # dropping `capabilities`, emptying the list, or leaving bytes `json.loads`
    # refuses (whose failure was swallowed to `{}`) each gave `wanted = set()`
    # and passed ANY ceiling, all four with a fully valid signature and a
    # matching binding, because every one of them was signed over as staged. The
    # IR is not writable that way: it is re-hashed against the signed
    # `composition_hash` at (c) above, so any edit that lowers the derived
    # surface is a different composition and refuses there first.
    if trust.capability_ceiling is not None:
        try:
            wanted, _derived = capability_surface(ir)
        except RevlError as error:
            # Fail CLOSED. An unreadable surface is not an empty one.
            return _refusal(
                LINK_CAPABILITY,
                "this receiver cannot measure the capability surface of the "
                f"staged composition, so it cannot check its ceiling: {error}")
        over = sorted(wanted - set(trust.capability_ceiling))
        if over:
            return _refusal(
                LINK_CAPABILITY,
                f"the staged composition reaches {', '.join(over)}, outside "
                "this receiver's ceiling "
                f"({', '.join(sorted(trust.capability_ceiling)) or 'none'}); "
                "a deploy may not silently widen authority. Measured from the "
                f"signature-bound {IR_DOCUMENT}, not from {POLICY_NAME}.")
        # `policy.json` stays a checked projection rather than a decorative one:
        # if the chain binds it, it must agree with what the IR derives. It is
        # not what the ceiling was measured against, so a disagreement is a
        # tamper signal and not a bypass.
        if FACET_POLICY in bindings:
            try:
                recorded = json.loads(
                    (root / POLICY_NAME).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
                return _refusal(
                    LINK_POLICY,
                    f"the chain binds `{FACET_POLICY}` but the staged "
                    f"{POLICY_NAME} cannot be read as JSON ({error}); it is "
                    "refused rather than read as an empty policy")
            if isinstance(recorded, dict):
                projected = sorted({str(c)
                                    for c in (recorded.get("capabilities") or [])})
            else:
                projected = None
            if projected is None or set(projected) != wanted:
                shown = projected if projected is not None else repr(recorded)
                return _refusal(
                    LINK_POLICY,
                    f"the staged {POLICY_NAME} does not project the staged "
                    f"composition: it records {shown}, the {IR_DOCUMENT} this "
                    f"receiver admitted reaches {sorted(wanted)}")

    # (h) freshness: a signature proves authenticity, never current validity.
    if trust.evidence_ttl_seconds is not None:
        signed_at = _parse_timestamp(attestation.get("timestamp"))
        if signed_at is None:
            return _refusal(LINK_EVIDENCE,
                            "the attestation carries no readable timestamp, so "
                            "its freshness cannot be checked; refused fail-closed")
        current = now if isinstance(now, (int, float)) else time.time()
        age = current - signed_at
        if age > trust.evidence_ttl_seconds:
            return _refusal(
                LINK_EVIDENCE,
                f"the attestation is {age:.0f}s old, past this receiver's "
                f"{trust.evidence_ttl_seconds:.0f}s freshness TTL; a valid "
                "signature over stale evidence is still refused")

    receipt = {
        "kind": RECEIPT_KIND,
        "version": RECEIPT_VERSION,
        "verdict": ACCEPT,
        "backend": trust.backend,
        # Both hashes are the RE-COMPUTED ones. A receipt therefore binds what
        # this receiver actually holds, which is what makes a later lie about
        # what it loaded detectable and non-repudiable (§5-A2).
        "composition_hash": recomputed_ir,
        "artifact_hash": recomputed_artifact,
        "key_id": kid,
        "runtime_versions": dict(sorted((runtime_versions or {}).items())),
        "nonce": nonce or os.urandom(16).hex(),
        "admitted_at": attest._now_iso(None) if now is None else str(now),
    }
    if host_key is not None:
        receipt["signature"] = _receipt_mac(receipt, host_key)
    return receipt


def verify_receipt(receipt: Mapping, host_key: bytes) -> tuple[bool, str]:
    """Check a receipt's own signature — the receiver signed what it claims to
    hold, so an audit can attribute a lie rather than only notice one.

    The MAC is domain-tagged and the envelope is checked, so an item-127
    attestation signed with this key is NOT a receipt: it fails the MAC (a
    different domain) and, were it not for that, would fail on `kind`. A record
    saying "this composition was admitted" and a record saying "I admitted it"
    are different claims by different parties, and neither may stand in for the
    other."""
    given = receipt.get("signature")
    if not isinstance(given, str):
        return False, "receipt carries no signature"
    if not hmac.compare_digest(_receipt_mac(receipt, host_key), given):
        return False, "receipt signature mismatch"
    if receipt.get("kind") != RECEIPT_KIND:
        return False, (f"not a {RECEIPT_KIND} record: kind is "
                       f"{receipt.get('kind')!r}")
    if receipt.get("version") != RECEIPT_VERSION:
        return False, (f"receipt version is {receipt.get('version')!r}, "
                       f"expected {RECEIPT_VERSION!r}")
    if receipt.get("verdict") not in (ACCEPT, REFUSE):
        return False, f"receipt verdict is {receipt.get('verdict')!r}"
    return True, "receipt is authentic"


def render_receipt(receipt: Mapping) -> str:
    """One-line-per-fact rendering of an admission verdict."""
    if receipt.get("verdict") == ACCEPT:
        return "\n".join([
            f"admission: ACCEPT ({receipt.get('backend')})",
            f"  composition: {receipt.get('composition_hash')}",
            f"  artifact:    {receipt.get('artifact_hash')}  (re-hashed here)",
            f"  nonce:       {receipt.get('nonce')}",
        ])
    return "\n".join([
        f"admission: REFUSE — chain link `{receipt.get('link')}`",
        f"  {receipt.get('reason')}",
    ])


# ---------------------------------------------------------------------------
# §1.4. distributed effect correlation, authenticated against the peer
# ---------------------------------------------------------------------------

CORRELATION_FIELD = "correlation"
AUTH_FIELD = "auth"

#: Why a correlation envelope was rejected. Distinct reasons, because they are
#: distinct attacks: a peer speaking under someone else's name, a peer replaying
#: an envelope, a peer this receiver has no identity for.
REJECT_MALFORMED = "malformed-envelope"
REJECT_UNKNOWN_PEER = "unknown-peer"
REJECT_FORGED = "forged-envelope"
REJECT_PEER_MISMATCH = "peer-identity-mismatch"
REJECT_DUPLICATE = "duplicate-envelope"


@dataclass(frozen=True)
class Correlation:
    """The identity every call crossing a seam carries (§1.4).

    `peer_identity` is the item-55 per-process identity of the CALLER — the
    same token the mTLS certificate is minted for on a network seam. It is part
    of the envelope because dedup is scoped BY it (Addendum 3b): one peer's
    idempotency key namespace is not another's, so a peer can neither collide
    with nor replay a sibling's crossing.
    """

    composition_id: str
    generation: int
    peer_identity: str
    effect_id: str
    realm: str | None = None
    idempotency_key: str | None = None
    parent_effect: str | None = None

    def to_wire(self) -> dict:
        return {"composition_id": self.composition_id,
                "generation": int(self.generation),
                "peer_identity": self.peer_identity,
                "effect_id": self.effect_id,
                "realm": self.realm,
                "idempotency_key": self.idempotency_key,
                "parent_effect": self.parent_effect}

    @classmethod
    def from_wire(cls, wire: Mapping) -> "Correlation":
        return cls(composition_id=str(wire["composition_id"]),
                   generation=int(wire["generation"]),
                   peer_identity=str(wire["peer_identity"]),
                   effect_id=str(wire["effect_id"]),
                   realm=wire.get("realm"),
                   idempotency_key=wire.get("idempotency_key"),
                   parent_effect=wire.get("parent_effect"))

    def dedup_key(self) -> tuple:
        """The scope duplicate detection runs on: `(peer_identity,
        composition_id, generation, idempotency_key)` — exactly the tuple the
        design's binding rule names, no wider and no narrower."""
        return (self.peer_identity, self.composition_id,
                int(self.generation), self.idempotency_key)


def _envelope_bytes(wire: Mapping) -> bytes:
    """Canonical bytes of an envelope, excluding its own auth tag. Same
    sorted-keys/compact spelling `attest._canonical_bytes` uses, so the tag is a
    pure function of the envelope's content and not of member order."""
    body = {k: v for k, v in wire.items() if k != AUTH_FIELD}
    return json.dumps(body, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def seal(correlation: Correlation, secret: bytes) -> dict:
    """The wire envelope with its authentication tag: HMAC over the canonical
    envelope bytes under the CALLER's own per-process secret.

    That secret is what makes `peer_identity` a claim the receiver can check
    rather than a string the caller chose. On a network seam the mTLS peer
    certificate authenticates the same identity independently, and
    :meth:`CorrelationGuard.admit` requires the two to agree — so a leaked
    secret still cannot speak from the wrong TLS session, and a valid TLS
    session still cannot speak under another process's name.
    """
    wire = correlation.to_wire()
    wire[AUTH_FIELD] = hmac.new(bytes(secret), _envelope_bytes(wire),
                                hashlib.sha256).hexdigest()
    return wire


class DedupLedger:
    """Seen `(peer_identity, composition_id, generation, idempotency_key)`
    tuples. An envelope with no idempotency key is not deduplicated — there is
    nothing declaring the call re-deliverable (item 309), so silently dropping
    a repeat would be a guess."""

    def __init__(self) -> None:
        self._seen: set = set()

    def admit(self, correlation: Correlation) -> bool:
        """True the first time this scope is seen, False on a replay."""
        if correlation.idempotency_key is None:
            return True
        key = correlation.dedup_key()
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    def seen(self, correlation: Correlation) -> bool:
        return correlation.dedup_key() in self._seen

    def __len__(self) -> int:
        return len(self._seen)


class CorrelationGuard:
    """What a provider runs on every seam request before it dispatches.

    Holds the identity -> secret table the conductor distributed (one secret per
    process, in that process's spec, owner-only) and a :class:`DedupLedger`. It
    answers `(ok, reason)`; a provider turns a `False` into an error reply and
    never reaches the service.
    """

    def __init__(self, secrets: Mapping[str, bytes],
                 ledger: Optional[DedupLedger] = None) -> None:
        self._secrets = dict(secrets)
        self.ledger = ledger if ledger is not None else DedupLedger()

    def admit(self, wire: Any, *, transport_identity: str | None = None
              ) -> tuple[bool, str]:
        """Authenticate one envelope and check it is not a replay.

        Four gates, in the order that makes each meaningful:

          1. **shape** — an envelope missing a required member is refused, not
             defaulted;
          2. **known peer** — an identity this receiver holds no secret for
             cannot be authenticated, so it is refused rather than trusted;
          3. **authenticity** — the tag must verify under THAT peer's secret,
             and, when the transport authenticated an identity of its own (the
             mTLS peer certificate), the two must agree. Either half alone is
             forgeable by the other's holder; both together are not;
          4. **freshness** — a replay of an already-seen
             `(peer, composition, generation, idempotency_key)` is refused.
        """
        if not isinstance(wire, Mapping):
            return False, REJECT_MALFORMED
        try:
            correlation = Correlation.from_wire(wire)
        except (KeyError, TypeError, ValueError):
            return False, REJECT_MALFORMED
        secret = self._secrets.get(correlation.peer_identity)
        if secret is None:
            return False, REJECT_UNKNOWN_PEER
        given = wire.get(AUTH_FIELD)
        if not isinstance(given, str):
            return False, REJECT_FORGED
        expected = hmac.new(bytes(secret), _envelope_bytes(wire),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, given):
            return False, REJECT_FORGED
        if (transport_identity is not None
                and transport_identity != correlation.peer_identity):
            return False, REJECT_PEER_MISMATCH
        if not self.ledger.admit(correlation):
            return False, REJECT_DUPLICATE
        return True, "authenticated"


# ---------------------------------------------------------------------------
# §3. the coordinated cross-process commit/abort protocol
# ---------------------------------------------------------------------------

#: Per-participant outcomes. The vocabulary mirrors `bundle.verify`'s
#: OK/MISMATCH/cannot-verify: it never claims an atomicity it did not achieve.
NEVER_COMMITTED = "never-committed"
APPLIED = "applied"
ROLLED_BACK_CLEAN = "rolled-back-clean"
ROLLED_BACK_WITH_RESIDUE = "rolled-back-with-residue"
UNRESOLVED = "unresolved"

#: Aggregate verdicts.
DEPLOY_APPLIED = "applied"
DEPLOY_REFUSED = "refused"
DEPLOY_ABORTED_CLEAN = "aborted-clean"
DEPLOY_ABORTED_WITH_RESIDUE = "aborted-with-residue"

#: The protocol tag every report carries, so a reader can tell at a glance that
#: this was a coordinated protocol and NOT a lifted in-process theorem.
PROTOCOL = "coordinated-two-phase"


class Unreachable(RuntimeError):
    """A participant could not be reached for a phase. Its own inverses are in
    its own process; the coordinator cannot run them, so the honest report is
    `unresolved`, never `rolled-back`."""


class Participant:
    """One process taking part in a deploy.

    The coordinator talks to a participant and NOTHING ELSE. It does not hold
    the participant's inverses, cannot enumerate them, and never runs one — the
    whole reason §3.1 says the in-process rollback theorem does not cross a
    seam. `abort` is a *request*: the participant runs its own local LIFO
    unwind, in its own process, and reports what it settled to.
    """

    # Subclasses carry an `identity` (the item-55 per-process identity). It is
    # deliberately NOT declared here, even as a class attribute: a dataclass
    # subclass would inherit it as a defaulted field and be unable to declare
    # any required field of its own after it.

    def prepare(self) -> dict:  # pragma: no cover — interface
        raise NotImplementedError

    def commit(self) -> dict:  # pragma: no cover — interface
        raise NotImplementedError

    def abort(self) -> dict:  # pragma: no cover — interface
        raise NotImplementedError


@dataclass
class _LedgerEntry:
    """One row of the coordinator's commit ledger: WHO committed, in what
    ORDER, and the receipt they returned. Deliberately not an inverse — this is
    the entire cross-process state the coordinator is entitled to hold."""

    identity: str
    order: int
    receipt: dict


def run_deploy(participants: Sequence[Participant], *,
               approval_path: Optional[str] = None,
               federation_id: str = "deploy",
               generation: int = 0,
               on_event: Optional[Callable[[str, str, dict], None]] = None
               ) -> dict:
    """Drive PREPARE / COMMIT / ABORT across co-located participant processes.

    PREPARE is effect-free, so a refusal there is not a rollback at all: every
    participant is `never-committed` and the deploy fails clean (§3.2). This is
    the common failure case and it costs zero unwinding.

    COMMIT is where the coordination is real. Each participant applies its own
    slice with its own `apply`, in its own process, and the coordinator appends
    a row to its **commit ledger**. That ledger is the only cross-process state
    it holds — no inverses, no accumulator, nothing it could replay itself. On
    a failure it:

      1. records a durable ABORT decision (when `approval_path` is given), so a
         participant that is stranded mid-flight can settle against a record
         rather than a guess;
      2. sends ABORT to committed participants in REVERSE LEDGER ORDER — the
         cross-process LIFO is an ordering over *messages*, and each unwind is
         performed by the participant that owns the inverses;
      3. collects each participant's own settled verdict, and reports one it
         could not reach as `unresolved(...)`, never as rolled back.

    The aggregate verdict is `applied` only when every participant applied;
    `aborted-clean` only when every committed participant reported
    `rolled-back-clean`; otherwise `aborted-with-residue`, with the residue
    enumerated.
    """
    def emit(phase: str, identity: str, detail: dict) -> None:
        if on_event is not None:
            on_event(phase, identity, detail)

    outcomes: dict[str, dict] = {p.identity: {"outcome": NEVER_COMMITTED}
                                 for p in participants}
    prepared: list[dict] = []
    for participant in participants:
        try:
            report = participant.prepare()
        except Unreachable as error:
            report = {"ok": False, "reason": f"unreachable during PREPARE: {error}"}
        except Exception as error:  # noqa: BLE001 — any prepare failure refuses
            report = {"ok": False,
                      "reason": f"{type(error).__name__}: {error}"}
        emit("prepare", participant.identity, report)
        prepared.append(report)
        if not report.get("ok"):
            return {
                "protocol": PROTOCOL,
                "verdict": DEPLOY_REFUSED,
                "phase": "prepare",
                "refusedBy": participant.identity,
                "reason": report.get("reason"),
                "participants": outcomes,
                "commitLedger": [],
                "residue": {"clean": True, "outstanding": [],
                            "proof": "PREPARE has no runtime effects, so a "
                                     "refusal there is not a rollback: no "
                                     "participant activated anything and there "
                                     "is nothing to undo."},
            }

    ledger: list[_LedgerEntry] = []
    failure: Optional[dict] = None
    for participant in participants:
        try:
            report = participant.commit()
        except Unreachable as error:
            report = {"ok": False, "reason": f"unreachable during COMMIT: {error}",
                      "unresolved": True}
        except Exception as error:  # noqa: BLE001
            report = {"ok": False, "reason": f"{type(error).__name__}: {error}"}
        emit("commit", participant.identity, report)
        if report.get("ok"):
            ledger.append(_LedgerEntry(participant.identity, len(ledger), report))
            outcomes[participant.identity] = {"outcome": APPLIED, "receipt": report}
            continue
        if report.get("unresolved"):
            # We asked it to commit and never heard back. It may or may not have
            # applied; only that process's own WAL settles it.
            outcomes[participant.identity] = {
                "outcome": UNRESOLVED, "reason": report.get("reason"),
                "settleWith": "run `revl recover` on that participant's own WAL"}
        else:
            # Its LOCAL apply failed, so it ALREADY unwound its own slice LIFO
            # and proved its own no-residue before answering (apply.py, per
            # process). Nothing cross-process to do for this one.
            outcomes[participant.identity] = {
                "outcome": NEVER_COMMITTED, "reason": report.get("reason"),
                "localRollback": report.get("residue")}
        failure = {"identity": participant.identity,
                   "reason": report.get("reason")}
        break

    if failure is None:
        if approval_path is not None:
            write_commit_approval(approval_path, federation_id=federation_id,
                                  generation=generation,
                                  participants=[p.identity for p in participants])
        return {
            "protocol": PROTOCOL,
            "verdict": DEPLOY_APPLIED,
            "phase": "commit",
            "participants": outcomes,
            "commitLedger": [{"identity": e.identity, "order": e.order}
                             for e in ledger],
            "residue": {"clean": True, "outstanding": [],
                        "proof": "every participant applied its own slice and "
                                 "returned a receipt; nothing was aborted."},
        }

    # Durable decision FIRST: a participant stranded from here on settles by
    # reading a record, never by guessing (Addendum 2 / §3.4).
    if approval_path is not None:
        write_abort_decision(approval_path, federation_id=federation_id,
                             generation=generation, reason=failure["reason"],
                             failed=failure["identity"])

    by_identity = {p.identity: p for p in participants}
    unresolved: list[dict] = []
    residue: list[dict] = []
    for entry in reversed(ledger):        # reverse commit order: the cross-
        participant = by_identity[entry.identity]   # process LIFO, over MESSAGES
        try:
            report = participant.abort()
        except Unreachable as error:
            outcomes[entry.identity] = {
                "outcome": UNRESOLVED, "reason": str(error),
                "lastReceipt": entry.receipt,
                "settleWith": "run `revl recover` on that participant's own WAL"}
            unresolved.append({"identity": entry.identity, "reason": str(error)})
            emit("abort", entry.identity, {"ok": False, "unresolved": True})
            continue
        except Exception as error:  # noqa: BLE001
            outcomes[entry.identity] = {
                "outcome": UNRESOLVED, "reason": f"{type(error).__name__}: {error}",
                "lastReceipt": entry.receipt}
            unresolved.append({"identity": entry.identity, "reason": str(error)})
            emit("abort", entry.identity, {"ok": False, "unresolved": True})
            continue
        emit("abort", entry.identity, report)
        clean = bool((report.get("residue") or {}).get("clean"))
        outcomes[entry.identity] = {
            "outcome": ROLLED_BACK_CLEAN if clean else ROLLED_BACK_WITH_RESIDUE,
            "residue": report.get("residue"),
            # Recorded per participant: the unwind ran THERE, in that process.
            "unwoundBy": report.get("pid"),
        }
        if not clean:
            residue.append({"identity": entry.identity,
                            "outstanding": (report.get("residue") or {}).get("outstanding")})

    clean = not unresolved and not residue
    return {
        "protocol": PROTOCOL,
        "verdict": DEPLOY_ABORTED_CLEAN if clean else DEPLOY_ABORTED_WITH_RESIDUE,
        "phase": "abort",
        "failedAt": failure["identity"],
        "reason": failure["reason"],
        "participants": outcomes,
        "commitLedger": [{"identity": e.identity, "order": e.order} for e in ledger],
        "abortOrder": [e.identity for e in reversed(ledger)],
        "residue": {
            "clean": clean,
            "outstanding": unresolved + residue,
            "proof": _abort_proof(ledger, unresolved, residue),
        },
    }


def _abort_proof(ledger: list, unresolved: list, residue: list) -> str:
    if not ledger:
        return ("no participant had committed when the failure was reported, so "
                "the abort had nothing to unwind.")
    if not unresolved and not residue:
        return (f"{len(ledger)} committed participant(s) were sent ABORT in reverse "
                "commit order; each ran its OWN local LIFO unwind in its own "
                "process and proved its own no-residue. The coordinator held only "
                "the commit ledger — it never held or ran an inverse.")
    parts = []
    if unresolved:
        parts.append(f"{len(unresolved)} participant(s) UNRESOLVED (unreachable "
                     "for ABORT). Their inverses live in their own processes, so "
                     "this is reported, not resolved: settle each from its own WAL")
    if residue:
        parts.append(f"{len(residue)} participant(s) rolled back but reported "
                     "residue of their own")
    return "; ".join(parts) + ". No participant is claimed rolled back that did not say so."


# ---------------------------------------------------------------------------
# co-located federation atomicity (the second CRITICAL)
# ---------------------------------------------------------------------------

#: The durable record whose PRESENCE authorizes crossing an irreversible effect.
#: Deliberately the same spelling `recovery.py` already keys roll-forward off
#: (`commit-approved`), prefixed for the federation scope, so `settle_stranded`
#: can hand the rule to `recovery.recover` VERBATIM rather than re-deciding it.
FEDERATION_APPROVED = "federation-commit-approved"
FEDERATION_ABORTED = "federation-abort-decided"

#: Why a plan is refused admission into a federation update.
REFUSE_IRREVERSIBLE = "non-deferrable-irreversible-crossing"
REFUSE_CONTRACT = "contract-break"


def reached_emissions(ir: dict) -> list[dict]:
    """Every emission-extern CALL SITE a composition reaches, with whether it is
    deferrable.

    Reachability, not declaration — the same discipline
    `session_commit._reached_deferred_calls` uses, and for the same reason: a
    declared-but-never-called extern crosses nothing. An `emission` extern
    carrying `deferred` is item 245's class (b): PREPARE can HOLD it in the
    deferral queue and flush it only after the durable federation decision, so
    it is deferrable. A bare `emission` extern is class (c) — it fires at the
    call, with no inverse — so reaching one means the composition NECESSARILY
    crosses an irreversible effect during its local commit. `witnessed` and
    `acquire` externs are absent from this list by construction: their
    reversibility is a registered inverse or an effect bracket.
    """
    externs = {e["name"]: e for e in ir.get("externs") or []
               if e.get("class") == "emission"}
    if not externs:
        return []
    found: list[dict] = []
    seen: set = set()

    def walk(node, component: str) -> None:
        if isinstance(node, dict):
            if node.get("step") == "emit":
                expr = node.get("expr") or {}
                name = expr.get("name")
                if expr.get("kind") == "fn" and name in externs:
                    mark = (component, name)
                    if mark not in seen:
                        seen.add(mark)
                        found.append({
                            "component": component, "extern": name,
                            "deferrable": bool(externs[name].get("deferred")),
                            "idempotency_key": externs[name].get("idempotency_key"),
                        })
            for value in node.values():
                walk(value, component)
        elif isinstance(node, list):
            for item in node:
                walk(item, component)

    for comp in ir.get("components") or []:
        walk(comp, comp.get("name") or "?")
    return found


def federation_admission(plans: Mapping[str, dict], *,
                         contracts: Optional[Sequence[tuple]] = None) -> dict:
    """Admit — or REFUSE — an all-or-nothing update across co-located
    compositions.

    `plans` maps a composition id to the IR it would move to. `contracts` is an
    optional sequence of `(consumer_surface_doc, provider_composition_id)`
    pairs; each is checked with `federation.check` against the provider's NEW
    IR, so a federation update that would break a pinned consumer surface is
    refused as one unit rather than deployed composition by composition.

    The refusal that matters is the second CRITICAL's: **a plan that
    necessarily crosses a non-deferrable irreversible effect is REFUSED
    admission rather than silently degrading atomicity.** Such a plan cannot
    honour "no composition crosses an irreversible effect before a durable
    federation-commit-approved record exists" — PREPARE has no way to hold a
    class-(c) crossing, so admitting it would leave a late partition able to
    strand that composition with residue while its peers reverted. Refusing is
    the only honest option; the fix is to declare the extern `deferred`
    (item 245 class (b)) so PREPARE can hold it.

    Returns `{"admitted": bool, "refusals": [...], "deferred": [...]}`.
    """
    refusals: list[dict] = []
    deferred: list[dict] = []
    for composition_id in sorted(plans):
        for crossing in reached_emissions(plans[composition_id]):
            row = {"composition": composition_id, **crossing}
            if crossing["deferrable"]:
                deferred.append(row)
                continue
            refusals.append({
                "kind": REFUSE_IRREVERSIBLE,
                **row,
                "reason": (
                    f"`{composition_id}` necessarily crosses the irreversible "
                    f"emission `{crossing['extern']}` in `{crossing['component']}` "
                    "during its local commit. PREPARE cannot hold a class-(c) "
                    "crossing, so this composition could cross before a durable "
                    f"`{FEDERATION_APPROVED}` record exists and be stranded with "
                    "residue while its peers revert. Refused rather than "
                    "silently degrading the federation's atomicity — declare the "
                    "extern `deferred` (item 245 class (b)) so PREPARE can hold "
                    "it."),
            })
    for consumer_doc, provider_id in (contracts or ()):
        provider_ir = plans.get(provider_id)
        if provider_ir is None:
            continue
        from .federation import check  # noqa: PLC0415 — lazy, avoids a cycle
        verdict = check(consumer_doc, provider_ir)
        if not verdict["satisfied"]:
            refusals.append({
                "kind": REFUSE_CONTRACT,
                "composition": provider_id,
                "consumer": verdict.get("consumer"),
                "breaks": verdict["breaks"],
                "reason": (f"the federation update would break {verdict.get('consumer')}"
                           f"'s pinned surface on `{provider_id}`; an all-or-nothing "
                           "update refuses as one unit, it does not deploy the "
                           "compatible half."),
            })
    return {"admitted": not refusals, "refusals": refusals, "deferred": deferred}


def _fsync_append(path: str, record: dict) -> None:
    """Append one durable record, fsync'd — the same discipline the WAL writer
    and `recovery._append_discharge` use. Appending fires nothing, so it is safe
    on any path."""
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except (OSError, ValueError):  # pragma: no cover — e.g. a pipe target
            pass


def write_commit_approval(path: str, *, federation_id: str, generation: int,
                          participants: Sequence[str]) -> dict:
    """Make the federation's COMMIT decision durable, BEFORE any participant is
    allowed to flush a held irreversible emission. This record's existence is
    the whole anti-split-brain property: every stranded participant reads the
    same record and reaches the same verdict."""
    record = {"record": FEDERATION_APPROVED, "federation": federation_id,
              "generation": int(generation),
              "participants": sorted(participants),
              "at": attest._now_iso(None)}
    _fsync_append(path, record)
    return record


def write_abort_decision(path: str, *, federation_id: str, generation: int,
                         reason: str | None, failed: str | None = None) -> dict:
    """Make the federation's ABORT decision durable. Written BEFORE any abort
    message goes out, so a participant stranded during the abort still settles
    against a record."""
    record = {"record": FEDERATION_ABORTED, "federation": federation_id,
              "generation": int(generation), "reason": reason,
              "failed": failed, "at": attest._now_iso(None)}
    _fsync_append(path, record)
    return record


def read_decision(path: str) -> tuple[str | None, dict | None]:
    """Read the durable federation decision: `(FEDERATION_APPROVED |
    FEDERATION_ABORTED | None, record)`.

    A file that does not exist is `None` — the decision was never made, which
    is a definite answer (roll back). A file that exists but cannot be parsed
    raises: that is NOT a definite answer, and guessing between roll-forward and
    roll-back is exactly the split-brain the record exists to prevent.
    """
    if not os.path.exists(path):
        return None, None
    decision: str | None = None
    record: dict | None = None
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)   # a torn decision record fails closed
            kind = entry.get("record")
            if kind in (FEDERATION_APPROVED, FEDERATION_ABORTED):
                decision, record = kind, entry
    return decision, record


def settle_stranded(wal_path: str, decision_path: str, *, world=None) -> dict:
    """Settle a participant that was stranded mid-federation-update.

    The rule is `recovery.py`'s, applied VERBATIM — this function decides
    nothing on its own:

      * the durable `federation-commit-approved` record is PRESENT: the
        federation committed. The participant's own WAL is given the
        `commit-approved` marker recovery already keys roll-forward off, and
        `recovery.recover` rolls it forward (replaying no inverse, reporting any
        owed flush honestly).
      * the record is ABSENT (or an explicit abort decision): the federation did
        not commit. `recovery.recover` reads the WAL unchanged and rolls back
        LIFO, reporting residue exactly as it does today.
      * the record is UNREADABLE: there is no definite answer. Fails CLOSED with
        `unresolved` and settles nothing — a guess here is the split-brain.
    """
    from .recovery import recover  # noqa: PLC0415 — lazy, keeps import cheap

    try:
        decision, record = read_decision(decision_path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        return {
            "verdict": UNRESOLVED,
            "decision": (
                "the durable federation decision record could not be read "
                f"({error}). Recovery will NOT guess between roll-forward and "
                "roll-back: guessing is exactly the split-brain the record "
                "exists to prevent. Fails closed."),
            "residue": {"clean": False, "outstanding": [decision_path],
                        "proof": "no decision was read, so nothing was settled."},
        }
    if decision == FEDERATION_APPROVED:
        # Hand recovery the marker it already keys roll-forward off, then let
        # recovery.py decide. The rule is not re-implemented here.
        _fsync_append(wal_path, {"record": "commit-approved",
                                 "hash": (record or {}).get("federation"),
                                 "federation": (record or {}).get("federation"),
                                 "generation": (record or {}).get("generation")})
        settled = recover(wal_path, world=world)
        settled["federationDecision"] = FEDERATION_APPROVED
        return settled
    settled = recover(wal_path, world=world)
    settled["federationDecision"] = decision or "none"
    return settled


# ---------------------------------------------------------------------------
# a participant driven over a child process's stdin/stdout
# ---------------------------------------------------------------------------


@dataclass
class ProcessParticipant(Participant):
    """A :class:`Participant` that is a real OS process, spoken to over
    newline-delimited JSON on its stdin/stdout — the same control-channel shape
    `_process_runner.py` already uses for `repoint`.

    Every effect, every inverse and the whole durable WAL live in the CHILD.
    This object holds a pipe and a name. That is the point: when `abort` is
    sent, the unwind happens over there, and this side learns only what the
    child reports.
    """

    identity: str
    process: Any
    timeout: float = 20.0

    def _rpc(self, op: str, **payload) -> dict:
        proc = self.process
        if proc.poll() is not None or proc.stdin is None or proc.stdin.closed:
            raise Unreachable(
                f"participant {self.identity!r} is gone (exit "
                f"{proc.poll()}); its inverses were in that process, so this "
                "side cannot run them")
        try:
            proc.stdin.write(json.dumps({"op": op, **payload}) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError) as error:
            raise Unreachable(
                f"participant {self.identity!r} closed its control channel: "
                f"{error}") from None
        line = proc.stdout.readline()
        if not line:
            raise Unreachable(
                f"participant {self.identity!r} answered nothing to {op!r} "
                "(it died mid-phase); only its own WAL can settle it")
        return json.loads(line)

    def prepare(self) -> dict:
        return self._rpc("prepare")

    def commit(self) -> dict:
        return self._rpc("commit")

    def abort(self) -> dict:
        return self._rpc("abort")

    def status(self) -> dict:
        return self._rpc("status")

    def stop(self) -> None:
        proc = self.process
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=self.timeout)
        except Exception:  # noqa: BLE001 — best-effort teardown
            proc.kill()

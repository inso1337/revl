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

   A **network** seam cannot carry the SEALING half: the per-process secret is
   minted per boot by one conductor and a cross-composition peer runs under
   another, so demanding a sealed envelope there refuses the legitimate caller.
   What mTLS *does* give is a proven peer identity, and what was missing was a
   closed set to check it against — :class:`PeerAllowlist` (§1.4b). It also
   gives the dedup scope for free: the identity a replay check keys on is the
   one the handshake proved, so :class:`TransportReplayGuard` refuses a replay
   there with no shared secret at all. The achieved level is recorded per seam
   as :class:`SeamAdmission` (`sealed` / `peer-bound` / `peer-pinned` /
   `unverified`), because a weaker property under the same name would be the
   lie this module is written not to tell.

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
from collections import OrderedDict
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

#: The one item-31 gauntlet verdict that is evidence of admissibility, and the
#: dossier member naming the composition the dossier was graded over. Both are
#: `bundle.py`'s spellings, re-stated here so admission never imports the
#: bundler (the receiver has a bundle, not a build).
GAUNTLET_ADMISSIBLE = "admissible"
GAUNTLET_IDENTITY = "compositionHash"


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


def _artifact_files(root: Path) -> list[Path]:
    """Every REGULAR file under `root`, walked without following a single link.

    `rglob` plus `p.is_file()` followed symlinks, so a link inside
    `emitted/<backend>/` bound bytes that live OUTSIDE the bundle and stay
    writable after the signature is taken: the digest covered a path the bundle
    does not own (roadmap 428 F11). A tree digest is only a statement about the
    bundle if every byte it folds is IN the bundle, so a link is refused rather
    than resolved. The same refusal covers a fifo, socket or device node, whose
    "bytes" are not bytes the receiver can re-read and get the same answer.
    """
    if root.is_symlink():
        raise RevlError(str(root), 0,
                        "the emitted artifact directory is a symlink; a digest "
                        "over it would bind bytes the bundle does not contain")
    found: list[Path] = []
    stack = [root]
    while stack:
        for entry in sorted(stack.pop().iterdir()):
            if entry.is_symlink():
                raise RevlError(
                    str(entry), 0,
                    "the emitted artifact tree contains a symlink, so the "
                    "bytes it binds live outside the bundle and stay writable "
                    "after the signature is taken; it is refused rather than "
                    "followed")
            if entry.is_dir():
                stack.append(entry)
            elif entry.is_file():
                found.append(entry)
            else:
                raise RevlError(
                    str(entry), 0,
                    "the emitted artifact tree contains an entry that is not a "
                    "regular file, so it has no stable bytes to bind")
    return sorted(found)


def artifact_digest(backend_dir: Path | str) -> str:
    """The digest of one backend's emitted artifact — a *tree* digest, since a
    backend may emit several files (the wasm emitter emits one `.wat` per
    module).

    Canonical by construction: the relative path of each file and its bytes are
    folded in sorted-path order with explicit length framing, so no rename or
    split of the emitted set can collide with another. This is the value the
    signer binds and the value the receiver RE-COMPUTES from the bytes it is
    about to execute.

    Two things it REFUSES rather than digests, both `RevlError` (428 F11):

      * a symlink anywhere in the tree, or a `backend_dir` that is itself one —
        the bytes would live outside the bundle and stay writable after
        signing, so the digest would not be a statement about the bundle;
      * an EMPTY tree — it folds nothing and yields `sha256(b"")`, a value
        anyone can produce without holding any artifact at all. An artifact of
        no bytes is not an artifact, and admitting one means admitting a bundle
        with nothing to execute on the strength of a matching hash.
    """
    root = Path(backend_dir)
    files = _artifact_files(root)
    if not files:
        raise RevlError(
            str(root), 0,
            "the emitted artifact directory is empty, so its tree digest is "
            f"sha256 of nothing ({_sha256_bytes(b'')[:12]}…) — a value that "
            "matches without any artifact being present. An empty artifact is "
            "refused, never digested")
    digest = hashlib.sha256()
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

    A facet whose file the bundle does not carry is simply absent from the
    bindings. An `emitted/<backend>/` that EXISTS but holds no bytes, or that
    reaches outside the bundle through a link, is not an absent facet: it is a
    bundle that cannot be honestly signed, and :func:`artifact_digest` raises
    rather than binding it (roadmap 428 F11).

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

#: How far into the RECEIVER's future a signed timestamp may sit and still be
#: read as a real instant rather than a post-date. Freshness was anchored on a
#: signer-chosen field with a lower bound only, so a 2019 stamp was refused as
#: stale and a 2099 stamp ACCEPTED — its age is negative, and nothing compared
#: a negative age against anything (roadmap 428 F7). The bound exists because
#: two clocks are never identical, not because a signer may choose its own
#: window: it is a tolerance, and it is the receiver's, not the record's.
DEFAULT_CLOCK_SKEW_SECONDS = 300.0


def _receipt_mac(body: Mapping, host_key: bytes) -> str:
    """The receipt MAC: domain-tagged HMAC-SHA256 over the canonical body bytes
    (`body` is the receipt with its `signature` member removed).

    `ensure_ascii=True` here, unlike the attestation spelling, so a lone
    surrogate escapes rather than failing to encode. It can still meet a value
    that will not serialize at all, which is why this raises
    `attest.NotCanonicalizable` and :func:`verify_receipt` refuses on it."""
    try:
        payload = json.dumps(
            {k: v for k, v in body.items() if k != "signature"},
            sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise attest.NotCanonicalizable(
            f"the receipt has no canonical byte spelling: {error}") from error
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

    `clock_skew_seconds` and `not_before` are the receiver's half of freshness.
    `evidence_ttl_seconds` alone bounded the PAST only, so post-dating an
    attestation walked straight past it: the age came out negative and nothing
    compared a negative age with anything. `clock_skew_seconds` is how far into
    this receiver's future a signed instant may sit before it is read as a
    post-date rather than as two clocks disagreeing. `not_before` is an
    independent anchor the receiver SUPPLIES rather than reads off the record
    (a unix timestamp or an ISO-8601 string, typically the instant of the last
    deploy it accepted): evidence older than it is refused whatever its TTL
    says, which is what makes replaying an old-but-in-window attestation fail.

    `require_gauntlet` demands that the signed chain actually carry item-31
    gauntlet evidence. Off (the default) a bundle built where the gauntlet
    machinery was unavailable still admits, since `build_bundle` degrades
    honestly and stages no dossier; on, a chain with no `gauntlet` facet is
    REFUSED rather than admitted on absent evidence. A dossier that IS bound is
    read in full either way: an unreadable one, a verdict that is not
    `admissible`, and one graded over another composition are all refusals.

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
    require_gauntlet: bool = False
    clock_skew_seconds: float = DEFAULT_CLOCK_SKEW_SECONDS
    not_before: Optional[float | str] = None

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
    try:
        recomputed_ir = attest.canonical_hash(ir)
    except attest.NotCanonicalizable as error:
        # Staged bytes this receiver cannot canonicalize are unmeasurable, and
        # an unmeasurable input refuses (roadmap 428 F10/F11). `json.loads`
        # will build a lone surrogate out of a `"\ud800"` escape, so a staged
        # `ir/ir.json` can reach this.
        return _refusal(LINK_COMPOSITION,
                        f"the staged {IR_DOCUMENT} cannot be canonically "
                        f"hashed, so it cannot be compared with the signed "
                        f"composition: {error}")
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
    # Fail CLOSED on a tree this receiver cannot honestly digest: a symlink
    # binding bytes outside the bundle, a non-regular entry, or an empty
    # directory whose digest is `sha256(b"")` and therefore matches without any
    # artifact being present (roadmap 428 F11).
    try:
        recomputed_artifact = artifact_digest(backend_dir)
    except RevlError as error:
        return _refusal(
            LINK_ARTIFACT,
            f"the bytes staged at emitted/{trust.backend}/ cannot be digested "
            f"as an artifact this receiver holds: {error}")
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

    # (f2) the gauntlet VERDICT, and the composition it was graded over.
    #
    # (f) above only re-hashes the dossier: it proves the bytes are the signed
    # bytes and says nothing about what they SAY. So a dossier recording
    # `rejected` — the bundle's own evidence that it should not have been built
    # — was hashed, matched, and ACCEPTED, and a genuine `admissible` record
    # produced for a DIFFERENT artifact was accepted too, because the dossier
    # named no composition and there was nothing to check it against.
    # `bundle.build_bundle` now stamps the graded composition into the dossier
    # as it stages it, and both halves are read here.
    #
    # Fail closed throughout: a dossier that will not parse, or that names no
    # composition, is refused rather than read as absent evidence.
    if FACET_GAUNTLET in bindings:
        try:
            dossier = json.loads(
                (root / GAUNTLET_NAME).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
            return _refusal(
                FACET_GAUNTLET,
                f"the chain binds `{FACET_GAUNTLET}` but the staged "
                f"{GAUNTLET_NAME} cannot be read as JSON ({error}); unreadable "
                "evidence is refused, never read as evidence that passed")
        if not isinstance(dossier, dict):
            return _refusal(
                FACET_GAUNTLET,
                f"the staged {GAUNTLET_NAME} is not a gauntlet dossier "
                f"(it is a {type(dossier).__name__}, not an object)")
        verdict = dossier.get("verdict")
        if verdict != GAUNTLET_ADMISSIBLE:
            return _refusal(
                FACET_GAUNTLET,
                f"the staged {GAUNTLET_NAME} records the gauntlet verdict "
                f"{verdict!r}, not {GAUNTLET_ADMISSIBLE!r}: the bundle carries "
                "its own evidence that it should not have been built, and an "
                "honest signature over that evidence does not turn it into a "
                "pass")
        graded = dossier.get(GAUNTLET_IDENTITY)
        if not isinstance(graded, str) or not graded:
            return _refusal(
                FACET_GAUNTLET,
                f"the staged {GAUNTLET_NAME} carries no `{GAUNTLET_IDENTITY}`, "
                "so it names no composition: it cannot be shown to be evidence "
                "about the artifact in hand, and unidentified evidence is "
                "refused rather than assumed to be about this one")
        if not hmac.compare_digest(graded, recomputed_ir):
            return _refusal(
                FACET_GAUNTLET,
                f"the staged {GAUNTLET_NAME} was graded over composition "
                f"{graded[:12]}…, but the composition staged here is "
                f"{recomputed_ir[:12]}…: a genuine dossier for a DIFFERENT "
                "artifact hashes correctly into the chain and would otherwise "
                "ride along under an honest signature")
    elif trust.require_gauntlet:
        return _refusal(
            FACET_GAUNTLET,
            "this receiver requires item-31 gauntlet evidence and the signed "
            f"chain binds no `{FACET_GAUNTLET}` facet; absent evidence is "
            "refused, never counted as evidence that passed")

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
    #
    # The timestamp is SIGNER-CHOSEN and `attest._now_iso` passes an ISO string
    # straight through, so post-dating is a one-argument change. A TTL alone
    # bounds only the past: a 2099 stamp gave a NEGATIVE age, which is not
    # greater than any TTL, so it was accepted while a 2019 stamp was refused.
    # Both ends are bounded here, and both bounds are the RECEIVER's — its own
    # clock, its own skew tolerance, its own `not_before` anchor.
    if (trust.evidence_ttl_seconds is not None
            or trust.not_before is not None):
        signed_at = _parse_timestamp(attestation.get("timestamp"))
        if signed_at is None:
            return _refusal(LINK_EVIDENCE,
                            "the attestation carries no readable timestamp, so "
                            "its freshness cannot be checked; refused fail-closed")
        current = now if isinstance(now, (int, float)) else time.time()
        age = current - signed_at
        skew = max(float(trust.clock_skew_seconds), 0.0)
        if age < -skew:
            return _refusal(
                LINK_EVIDENCE,
                f"the attestation is dated {-age:.0f}s in this receiver's "
                f"FUTURE, past its {skew:.0f}s clock-skew tolerance: the "
                "timestamp is signer-chosen, so a post-dated record would "
                "otherwise stay 'fresh' indefinitely and outlive any TTL")
        if (trust.evidence_ttl_seconds is not None
                and age > trust.evidence_ttl_seconds):
            return _refusal(
                LINK_EVIDENCE,
                f"the attestation is {age:.0f}s old, past this receiver's "
                f"{trust.evidence_ttl_seconds:.0f}s freshness TTL; a valid "
                "signature over stale evidence is still refused")
        if trust.not_before is not None:
            floor = (trust.not_before if isinstance(trust.not_before, (int, float))
                     else _parse_timestamp(trust.not_before))
            if floor is None:
                return _refusal(
                    LINK_EVIDENCE,
                    "this receiver's `not_before` anchor is not a readable "
                    f"instant ({trust.not_before!r}), so freshness cannot be "
                    "checked against it; refused fail-closed rather than "
                    "checked against no anchor")
            if signed_at < float(floor):
                return _refusal(
                    LINK_EVIDENCE,
                    "the attestation predates this receiver's own freshness "
                    "anchor, so it is evidence from before the last state this "
                    "receiver accepted; a replay of in-window evidence is "
                    "still a replay")

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
    try:
        expected = _receipt_mac(receipt, host_key)
    except attest.NotCanonicalizable as error:
        return False, f"receipt cannot be verified: {error}"
    if not hmac.compare_digest(expected, given):
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


def repin(bundle_dir: Path | str, receipt: Mapping) -> tuple[bool, str]:
    """Re-derive the composition hash and the artifact digest from the staged
    bytes and check them against an ACCEPT `receipt`. Answers `(ok, reason)`.

    :func:`admit` is PREPARE: it verifies and LOADS NOTHING, so everything it
    checked was checked at admission time and nothing re-checked it at the
    moment of execution. Between the two the staged tree is ordinary files on
    ordinary disk, so an ACCEPT receipt can end up describing bytes that have
    since changed or gone (roadmap 428 F11). This is the check a loader runs
    IMMEDIATELY before it executes: same two re-derivations `admit` ran,
    against the receipt this receiver itself signed rather than against the
    sender's attestation.

    It is not a substitute for `admit` and cannot be: a receipt binds only what
    the receiver already admitted. It closes the admit-to-load window, and
    nothing else.

    Fails CLOSED everywhere: a receipt that is not an ACCEPT, one carrying no
    hashes, an IR that cannot be read or canonicalized, and an artifact tree
    that cannot be honestly digested are each a refusal.
    """
    root = Path(bundle_dir)
    if receipt.get("verdict") != ACCEPT:
        return False, (f"the receipt verdict is {receipt.get('verdict')!r}, "
                       f"not {ACCEPT}: there is nothing it authorises loading")
    bound_ir = receipt.get("composition_hash")
    bound_artifact = receipt.get("artifact_hash")
    backend = receipt.get("backend")
    if not (isinstance(bound_ir, str) and isinstance(bound_artifact, str)
            and isinstance(backend, str) and bound_ir and bound_artifact
            and backend):
        return False, ("the receipt does not pin a composition, an artifact and "
                       "a backend, so there is nothing to re-pin against")
    try:
        recomputed_ir = attest.canonical_hash(staged_ir(root))
    except (RevlError, attest.NotCanonicalizable) as error:
        return False, f"the staged {IR_DOCUMENT} cannot be re-hashed: {error}"
    if not hmac.compare_digest(recomputed_ir, bound_ir):
        return False, (f"the staged {IR_DOCUMENT} changed since admission: the "
                       f"receipt pins {bound_ir[:12]}…, the bytes on disk now "
                       f"hash to {recomputed_ir[:12]}…")
    backend_dir = root / EMITTED_ROOT / backend
    if not backend_dir.is_dir():
        return False, (f"the admitted emitted/{backend}/ artifact is gone; the "
                       "receipt describes bytes this receiver no longer holds")
    try:
        recomputed_artifact = artifact_digest(backend_dir)
    except RevlError as error:
        return False, (f"the staged emitted/{backend}/ can no longer be "
                       f"digested as an artifact: {error}")
    if not hmac.compare_digest(recomputed_artifact, bound_artifact):
        return False, (f"the staged emitted/{backend}/ changed since admission: "
                       f"the receipt pins {bound_artifact[:12]}…, the bytes on "
                       f"disk now hash to {recomputed_artifact[:12]}…")
    return True, "the staged bytes are still the admitted ones"


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
    """Canonical bytes of an envelope, excluding its own auth tag. Literally
    `attest._canonical_bytes`, so the tag is a pure function of the envelope's
    content and not of member order — and so an envelope with no canonical
    spelling raises `attest.NotCanonicalizable` here rather than a raw
    `UnicodeEncodeError` out of a guard whose contract is `(ok, reason)`
    (roadmap 428 F10). This used to inline the same `json.dumps`, which is how
    the two copies drifted into having the same defect twice."""
    return attest._canonical_bytes({k: v for k, v in wire.items()
                                    if k != AUTH_FIELD})


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
        # An envelope with no canonical byte spelling (a lone surrogate, a
        # value that will not serialize) cannot be authenticated, so it is
        # MALFORMED. It is peer-supplied, and this method's contract is
        # `(ok, reason)`: a crash here would escape past every caller written
        # to read a verdict and take the seam down (roadmap 428 F10).
        try:
            envelope = _envelope_bytes(wire)
        except attest.NotCanonicalizable:
            return False, REJECT_MALFORMED
        expected = hmac.new(bytes(secret), envelope, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, given):
            return False, REJECT_FORGED
        if (transport_identity is not None
                and transport_identity != correlation.peer_identity):
            return False, REJECT_PEER_MISMATCH
        if not self.ledger.admit(correlation):
            return False, REJECT_DUPLICATE
        return True, "authenticated"


# ---------------------------------------------------------------------------
# §1.4b. the same question on a NETWORK seam, and the honest answer
# ---------------------------------------------------------------------------
#
# :class:`CorrelationGuard` above is four gates: shape, KNOWN PEER (a closed,
# enumerated identity table), AUTHENTICITY (an HMAC under that peer's own
# per-process secret, cross-checked against the transport identity when the
# transport proved one), and FRESHNESS (the dedup ledger). On a local UDS seam
# the transport authenticates nothing — `bridge.peer_identity` returns None for
# a Unix socket — so the HMAC is the *only* thing binding the claimed
# `peer_identity` to a real caller, and the ledger is the only replay defence.
# That is what "sealed" means, and it is why the guard is worth running there.
#
# A TCP+mTLS seam cannot carry the same property, and the reason is structural
# rather than an omission:
#
#   * The secret is `secrets.token_bytes(32)`, minted fresh by the conductor at
#     every `run_placement()` and delivered only inside the process specs of
#     ITS OWN children. A network provider may also be dialled by an item-151
#     cross-composition consumer, which runs under a DIFFERENT conductor, on a
#     different machine, and can never hold that secret. There is no
#     distribution channel for it, and inventing one is item 118 Slice 2's
#     replicated control plane, not this plane. Demanding a sealed envelope
#     from a peer that cannot produce one does not harden the seam, it refuses
#     the legitimate caller — the same mistake as installing the guard in front
#     of a consumer tier whose bridge cannot seal.
#
# What a network seam CAN carry is the gate the UDS seam cannot: the mTLS
# handshake proves the peer's identity with a CA-signed private key, per
# session, and `bridge.peer_identity` reads it off the certificate. What is
# missing there is not authentication but a CLOSED SET: `verify_mode =
# CERT_REQUIRED` against a shared CA admits every identity that CA ever signed,
# so the receiver knows *who* is calling and has no opinion about whether that
# one may. :class:`PeerAllowlist` is that opinion, and it needs no secret
# distribution at all — only names, which an operator can declare. It also
# needs nothing from the caller's bridge, which is why it works for a py
# consumer, a node consumer and a cross-composition stranger alike.
#
# The FRESHNESS gate is the part of this section that used to read "goes with
# the secret", and that was wrong. Re-derive it from what each gate needs:
#
#   * dedup needs a KEY that names one crossing, and an IDENTITY to scope that
#     key by, so one caller's idempotency-key namespace is not another's;
#   * the HMAC exists to bind a CLAIMED identity to a real caller, which is
#     load-bearing on UDS precisely because `bridge.peer_identity` is None
#     there — the claim is otherwise unchecked;
#   * on TCP+mTLS the transport has ALREADY bound that identity, per session,
#     with a CA-signed private key. So the scoping identity is available
#     without any shared secret: it is read off the peer certificate, not off
#     the request body.
#
# :class:`TransportReplayGuard` is that gate. It keys dedup on
# `(TRANSPORT identity, composition_id, generation, idempotency_key)` — the
# first member from the handshake, never from the payload, because a replayer
# controls its payload and does not control the mTLS session it speaks in. It
# holds NO secret, so it needs no distribution channel and refuses no
# cross-composition caller: an item-151 consumer under another conductor
# already holds the one credential this gate reads.
#
# It is still weaker than sealing in one direction, and the vocabulary says so:
# a crossing that declares no `idempotency_key` is not deduplicated (item 309 —
# nothing declares it re-deliverable), and the ledger is BOUNDED, so the window
# is finite. See :class:`BoundedReplayLedger` for exactly what that costs.

#: The transport proved no identity at all (no TLS, or a session with no peer
#: certificate). Distinct from :data:`REJECT_UNKNOWN_PEER`, which is a peer that
#: named itself and is not on the list.
REJECT_UNAUTHENTICATED_PEER = "unauthenticated-peer"

#: The peer-admission level a seam ACHIEVED — never the one it wanted. The
#: vocabulary is `bundle.verify`'s OK / cannot-verify: a surface that cannot
#: prove its property says so.
ADMISSION_SEALED = "sealed"
#: mTLS identity checked against a declared allowlist AND keyed crossings
#: replay-checked against that transport-proven identity. Stronger than
#: :data:`ADMISSION_PEER_PINNED`, weaker than :data:`ADMISSION_SEALED` (an
#: unkeyed crossing is not deduplicated, and the ledger window is finite).
ADMISSION_PEER_BOUND = "peer-bound"
ADMISSION_PEER_PINNED = "peer-pinned"
ADMISSION_UNVERIFIED = "unverified"

#: Rendered for the level, so a reader never has to remember which is stronger.
ADMISSION_MEANING = {
    ADMISSION_SEALED: "peer authenticated by per-process secret, replays refused",
    ADMISSION_PEER_BOUND: "mTLS peer identity checked against a declared allowlist, "
                          "and a keyed crossing replay-checked against that proven "
                          "identity over a bounded window",
    ADMISSION_PEER_PINNED: "mTLS peer identity checked against a declared allowlist; "
                           "NOT replay-checked",
    ADMISSION_UNVERIFIED: "cannot verify who may call",
}


class PeerAllowlist:
    """What a NETWORK provider runs on every connection before it answers one.

    The mTLS handshake has already proved an identity by the time this is
    consulted; this decides whether that identity is one the placement declared
    may call. It holds NAMES, not secrets — that is the whole reason it works
    across a composition boundary where :class:`CorrelationGuard` cannot.

    It answers `(ok, reason)` in the same shape the correlation guard does, so a
    provider treats the two verdicts identically. It deliberately does NOT
    dedup: an allowlist has no envelope to dedup on, and pretending otherwise
    would be the claim this whole plane exists to avoid.
    """

    def __init__(self, identities: Iterable[str]) -> None:
        self.identities = frozenset(str(i) for i in identities)

    def admit(self, transport_identity: str | None) -> tuple[bool, str]:
        if not transport_identity:
            return False, REJECT_UNAUTHENTICATED_PEER
        if transport_identity not in self.identities:
            return False, REJECT_UNKNOWN_PEER
        return True, "peer-pinned"

    def __len__(self) -> int:
        return len(self.identities)


#: The verdict a :class:`TransportReplayGuard` returns for a crossing it
#: admitted and DID dedup, and for one it admitted without deduping because the
#: caller declared no idempotency key. Two spellings, because they are two
#: different properties and a caller reading the verdict should not have to
#: guess which one it got.
ADMITTED_REPLAY_CHECKED = "replay-checked"
ADMITTED_UNKEYED = "admitted-unkeyed"

#: Default bound on :class:`BoundedReplayLedger`: keys remembered per peer, and
#: peers remembered at once. The product is the hard ceiling on entries, so the
#: state a long-lived provider carries is bounded by construction rather than by
#: hoping the seam goes quiet.
REPLAY_WINDOW_PER_PEER = 1024
REPLAY_WINDOW_PEERS = 64


class BoundedReplayLedger:
    """Seen `(composition_id, generation, idempotency_key)` scopes, per peer,
    with a HARD bound on both dimensions.

    :class:`DedupLedger` is an unbounded `set`, which is correct for the process
    seam it was written for (one boot, a known handful of local children) and
    wrong for a network provider that may answer a peer for weeks. So this one
    is two nested LRUs:

      * at most `per_peer` scopes remembered for one identity, oldest evicted
        first;
      * at most `max_peers` identities remembered at once, least-recently-used
        evicted first.

    **What eviction costs, stated rather than hidden.** A replay is refused only
    while the original crossing is still remembered. Past `per_peer` keyed
    crossings from the same identity, the oldest scope ages out and a replay of
    *that* crossing would be admitted again. :attr:`evicted` counts every
    forgotten scope, so an operator can see the window was exceeded instead of
    inferring it.

    The alternative — refusing everything once full — is not fail-closed, it is
    a self-service denial of service: any peer could fill the window and take
    the seam down for everyone. Bounded memory with a counted, finite window is
    the honest trade, and it is per-identity so a chatty peer cannot age out a
    quiet peer's history (which would otherwise hand a stolen-key replayer a
    way to make room for its own replay).
    """

    def __init__(self, per_peer: int = REPLAY_WINDOW_PER_PEER,
                 max_peers: int = REPLAY_WINDOW_PEERS) -> None:
        if per_peer < 1 or max_peers < 1:
            raise ValueError("a replay window of zero remembers nothing and "
                             "would admit every replay; give a positive bound")
        self.per_peer = int(per_peer)
        self.max_peers = int(max_peers)
        self.evicted = 0
        self._peers: "OrderedDict[str, OrderedDict[tuple, None]]" = OrderedDict()

    def admit(self, identity: str, scope: tuple) -> bool:
        """True the first time `scope` is seen for `identity`, False on a
        replay. Records the scope either way (a refused replay does not need
        re-recording; it is already there)."""
        seen = self._peers.get(identity)
        if seen is None:
            seen = OrderedDict()
            self._peers[identity] = seen
            while len(self._peers) > self.max_peers:
                _, dropped = self._peers.popitem(last=False)
                self.evicted += len(dropped)
        self._peers.move_to_end(identity)
        if scope in seen:
            return False
        seen[scope] = None
        while len(seen) > self.per_peer:
            seen.popitem(last=False)
            self.evicted += 1
        return True

    def seen(self, identity: str, scope: tuple) -> bool:
        return scope in self._peers.get(identity, ())

    def __len__(self) -> int:
        return sum(len(seen) for seen in self._peers.values())


class TransportReplayGuard:
    """Replay protection for a NETWORK seam, keyed on the identity the mTLS
    handshake proved (§1.4b).

    :class:`CorrelationGuard` cannot run here: its per-process secret is minted
    by one conductor for its own children, and an item-151 cross-composition
    caller can never hold it. This guard holds NO secret. It reads the scoping
    identity off the peer certificate — `bridge.peer_identity`, the value the
    transport proved — so it needs no distribution channel and refuses no
    legitimate caller that mTLS already admitted.

    Three gates:

      1. **a proven identity** — no transport identity means nothing to scope a
         key by, and scoping by a caller-asserted name would dedup in a
         namespace the caller chooses. Refused as
         :data:`REJECT_UNAUTHENTICATED_PEER` rather than keyed on the payload.
      2. **agreement** — when the crossing names a `peer_identity`, it must be
         the one the handshake proved. This is what stops peer B from replaying
         a captured envelope of peer A: verbatim it is a
         :data:`REJECT_PEER_MISMATCH`, and rewritten to B's own name it is
         simply B's own first crossing, in B's own namespace, which B was
         allowed to make anyway.
      3. **freshness** — a repeat of an already-seen `(transport identity,
         composition_id, generation, idempotency_key)` is
         :data:`REJECT_DUPLICATE`. The first member comes from the transport;
         only the last three are read off the envelope, and a caller varying
         those is not defeating the gate, it is declaring a different crossing
         (which it could do anyway by minting a fresh key — dedup answers
         re-DELIVERY, not a caller that means to call twice).

    A request with no correlation member, or one that declares no
    `idempotency_key`, is ADMITTED and not deduplicated: nothing declares it
    re-deliverable (item 309), and refusing it would refuse every caller built
    before this envelope existed — the cross-composition failure mode this
    plane exists to avoid. The verdict says which of the two it was
    (:data:`ADMITTED_REPLAY_CHECKED` / :data:`ADMITTED_UNKEYED`), so a seam
    never reports a freshness check it did not run.
    """

    def __init__(self, ledger: Optional[BoundedReplayLedger] = None, *,
                 per_peer: int = REPLAY_WINDOW_PER_PEER,
                 max_peers: int = REPLAY_WINDOW_PEERS) -> None:
        self.ledger = (ledger if ledger is not None
                       else BoundedReplayLedger(per_peer, max_peers))

    def admit(self, wire: Any, *, transport_identity: str | None = None
              ) -> tuple[bool, str]:
        if not transport_identity:
            return False, REJECT_UNAUTHENTICATED_PEER
        if wire is None:
            return True, ADMITTED_UNKEYED
        if not isinstance(wire, Mapping):
            return False, REJECT_MALFORMED
        try:
            correlation = Correlation.from_wire(wire)
        except (KeyError, TypeError, ValueError):
            return False, REJECT_MALFORMED
        if correlation.peer_identity != transport_identity:
            return False, REJECT_PEER_MISMATCH
        if correlation.idempotency_key is None:
            return True, ADMITTED_UNKEYED
        # NOTE the identity in this key: the one the handshake proved, not
        # `correlation.peer_identity`. They are equal here only because gate 2
        # just required it; keying on the transport value is what keeps that
        # true if gate 2 ever loosens.
        scope = (correlation.composition_id, int(correlation.generation),
                 correlation.idempotency_key)
        if not self.ledger.admit(transport_identity, scope):
            return False, REJECT_DUPLICATE
        return True, ADMITTED_REPLAY_CHECKED

    def __len__(self) -> int:
        return len(self.ledger)


@dataclass(frozen=True)
class SeamAdmission:
    """One seam's achieved peer-admission level, for the conductor's audit.

    `provider` is the process that serves the seam, `transport` is `"uds"` or
    `"tcp+mtls"`, `level` is one of the three above and `detail` says why that
    level and not a stronger one. `peers` is the closed set when there is one.
    """

    provider: str
    transport: str
    level: str
    detail: str
    peers: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return self.level != ADMISSION_UNVERIFIED

    def render(self) -> str:
        head = f"  seam admission {self.provider} ({self.transport}): "
        if self.level == ADMISSION_UNVERIFIED:
            head += f"UNVERIFIED — {self.detail}"
        else:
            head += f"{self.level} — {self.detail}"
        if self.peers:
            head += f"  peers: {', '.join(self.peers)}"
        return head


def render_seam_admissions(admissions: Sequence["SeamAdmission"]) -> list[str]:
    """The conductor's admission block: one line per cross-process seam in
    placement declaration order (so a reader finds a process by name, not by
    rank), then a count of the ones that proved nothing. An empty list renders
    nothing, so a placement with no cross-process seam prints no block."""
    if not admissions:
        return []
    unverified = [a for a in admissions if not a.verified]
    lines = [a.render() for a in admissions]
    if unverified:
        lines.append(
            f"  seam admission: {len(unverified)} of {len(admissions)} seam(s) "
            "UNVERIFIED — see the lines above; a seam is only as closed as the "
            "peer set it can name")
    return lines


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
#: A pinned contract naming a provider this update does not carry. It used to
#: be SKIPPED, in a function whose whole posture is refuse-as-one-unit: a pin
#: the federation cannot check is not a pin the federation satisfied (roadmap
#: 428 F13).
REFUSE_UNKNOWN_PROVIDER = "contract-provider-not-in-update"


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

    Read off `query.Composition`'s per-scope facts: the SAME surface `revl
    audit` prints and `erase_report._crossings` folds, so this gate can never
    disagree with the boundary the audit shows. Roadmap item 419b: the earlier
    walk keyed on an `emit` STEP wrapper carrying a `kind: "fn"` expression,
    and missed two shapes that are ordinary revl.

    * A provide-method whose body IS the emit (`fn put(k, v) = emit host(v)`)
      lowers to a `return` step (the `emit` marker does not survive), so a
      provider that necessarily crosses an irreversible emission was admitted.
      The scope facts key on the CALLED NAME instead, which is sound because
      `emit` is the only spelling that may reach an emission extern, and they
      also carry what a pure fn reaches transitively.
    * An emission mediated through a required service's capability-scoped
      operation (`emit store.put(..)` against `emission[host_put] fn put`) was
      not enumerated at all. It is now, tagged with `via` (`"<key>.<method>"`).

    A mediated crossing is counted only when the key resolves to NO local
    provider: an external/federated provider, item 151's shape. When the
    provider is a component of this same composition its provide-method scope
    is walked in its own right, with the extern's TRUE deferrability, so
    counting the consumer's call again would double-count and, worse, would
    downgrade a genuinely `deferred` extern to class (c) and refuse a plan that
    is fine. An unresolved key is class (c) by item 245 Decision 2, which
    `erase_report._crossings` already applies verbatim: deferral is a property
    of an extern DECLARATION and is not spellable on a service method, so the
    operation fires at the call and this composition cannot prove the far side
    holds it.

    Each row is `{component, extern, deferrable, idempotency_key, via}`; `via`
    is `None` for a direct call site. `extern` is the capability the service
    method declares (`emission[host_put]`); it is `None` for an `emission`
    method that names no capability, and `via` is then the only name available.
    """
    from .query import Composition  # noqa: PLC0415 - lazy, avoids a cycle

    index = Composition(ir)
    found: list[dict] = []
    seen: set = set()

    def add(component: str, extern, deferrable: bool, via) -> None:
        mark = (component, extern, via)
        if mark in seen:
            return
        seen.add(mark)
        decl = index.externs.get(extern) or {}
        found.append({
            "component": component, "extern": extern,
            "deferrable": deferrable,
            "idempotency_key": decl.get("idempotency_key"),
            "via": via,
        })

    for comp in ir.get("components") or []:
        component = comp.get("name") or "?"
        for scope_id in index.scopes_of.get(component) or []:
            facts = index.scopes[scope_id]["facts"]
            for fact in facts["externs"]:
                if fact.get("emission"):
                    add(component, fact["name"], bool(fact.get("deferred")), None)
            for fact in facts["emissions"]:
                key, method = fact["key"], fact["method"]
                if index.method_scope(component, key, method) is not None:
                    continue
                spec = (((index.services.get(fact.get("service")) or {})
                         .get("methods") or {}).get(method) or {})
                for capability in spec.get("capabilities") or [None]:
                    add(component, capability, False, f"{key}.{method}")
    return found


def federation_admission(plans: Mapping[str, dict], *,
                         contracts: Optional[Sequence[tuple]] = None) -> dict:
    """Admit — or REFUSE — an all-or-nothing update across co-located
    compositions.

    `plans` maps a composition id to the IR it would move to. `contracts` is an
    optional sequence of `(consumer_surface_doc, provider_composition_id)`
    pairs; each is checked with `federation.check` against the provider's NEW
    IR, so a federation update that would break a pinned consumer surface is
    refused as one unit rather than deployed composition by composition. A
    contract naming a provider `plans` does not carry is REFUSED, not skipped:
    it cannot be checked, and a pin that could not be checked is not a pin the
    update satisfied.

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
            # a mediated crossing names the service operation it goes through;
            # the author cannot find `host_put` by reading their own component,
            # they wrote `emit store.put(..)` (item 274's standard: a refusal
            # names something the author can act on).
            what = f"emission `{crossing['extern']}`" if crossing["extern"] \
                else "emission"
            where = (f"through `{crossing['via']}` in `{crossing['component']}`"
                     if crossing["via"] else f"in `{crossing['component']}`")
            fix = ("declare the extern `deferred` (item 245 class (b)) so "
                   "PREPARE can hold it")
            if crossing["via"]:
                fix = ("deferral is a property of an extern DECLARATION and is "
                       "not spellable on a service method, so this crossing "
                       "cannot be held: provide the key inside this composition "
                       "so its declaration is in scope, or split the operation "
                       "out of the federated update")
            refusals.append({
                "kind": REFUSE_IRREVERSIBLE,
                **row,
                "reason": (
                    f"`{composition_id}` necessarily crosses the irreversible "
                    f"{what} {where} "
                    "during its local commit. PREPARE cannot hold a class-(c) "
                    "crossing, so this composition could cross before a durable "
                    f"`{FEDERATION_APPROVED}` record exists and be stranded with "
                    "residue while its peers revert. Refused rather than "
                    f"silently degrading the federation's atomicity; {fix}."),
            })
    for consumer_doc, provider_id in (contracts or ()):
        provider_ir = plans.get(provider_id)
        if provider_ir is None:
            # Fail CLOSED. This used to `continue`, so a contract naming a
            # provider outside `plans` — a typo, a composition dropped from the
            # update, a rename on one side only — was checked against nothing
            # and the federation admitted. A pin that could not be checked is
            # not a pin that was satisfied, and "all or nothing" cannot mean
            # "all of the ones we happened to be able to look at".
            named = ", ".join(sorted(plans)) or "(none)"
            refusals.append({
                "kind": REFUSE_UNKNOWN_PROVIDER,
                "composition": provider_id,
                "reason": (
                    f"a pinned consumer surface names `{provider_id}` as its "
                    "provider, but this federation update carries no plan for "
                    f"it (updating: {named}); the pin cannot be checked, so it "
                    "is refused rather than skipped. An all-or-nothing update "
                    "refuses as one unit."),
            })
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

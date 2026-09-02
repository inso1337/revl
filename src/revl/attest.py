"""`revl attest` — cryptographic attestation of verified compositions
(roadmap item 127).

After the gauntlet/gate has verified a composition, the interesting artifact is
no longer *whether* it passed but a portable, tamper-evident record *that it
did*: a signed statement binding a specific composition — identified by a
canonical hash of its admitted IR — to a verdict, the guarantees that verdict
proves, a timestamp, and a signer identity. That record travels with the
component (attached to a release, a registry entry, an audit trail) and lets a
downstream consumer confirm, without re-running revl, that *this exact*
composition is the one that passed.

Design — deliberately dependency-free
-------------------------------------
Nothing here reaches past the Python standard library. Two primitives carry the
whole feature:

  * ``hashlib.sha256`` over a **canonical** serialization of the IR gives the
    content hash — the stable identity of the admitted artifact. Canonical
    means ``json.dumps(..., sort_keys=True, separators=(",", ":"))``, the same
    byte-stable IR spelling ``revl fmt`` already relies on
    (``formatter._canonical_ir``), so a composition attested from a ``.rvl``
    source and re-verified against its compiled IR hash to the same value.
  * ``hmac`` (also stdlib) over the canonical bytes of the whole attestation
    *payload* — hash, verdict, timestamp, guarantees, signer — gives a keyed
    signature. A symmetric, secret-keyed signature is enough for the model
    revl needs (a signer and a verifier who share a secret, e.g. a CI system
    and the registry it publishes to) and pulls in no crypto dependency.

The signature covers the payload, not just the IR hash, so tampering with the
verdict, the timestamp, or the recorded guarantees is caught exactly like a
tampered hash. The key never appears in the attestation: a non-secret
``key_id`` fingerprint (a truncated SHA-256 of the key) lets a verifier confirm
*which* key would be needed without disclosing it.

What the guarantee list MEANS (v2)
----------------------------------
Through envelope v1 the ``guarantees`` member was a CONSTANT: ``sorted()`` over
the diagnostics catalogue, emitted by a ``make_attestation`` that never
compiled anything. A composition the compiler refuses BY NAME for violating G2
still got a valid signature asserting G2 held. That is a signed claim that is
not true, so v2 changes what the member is:

  * ``make_attestation`` REFUSES to sign without a :class:`GateVerdict`
    produced by :func:`run_gate`, which runs the reference frontend
    (``compile_files``/``compile_source`` plus ``holes.refuse_admission`` — the
    same gate ``revl run`` applies) and hashes the document it admitted;
  * the verdict's ``composition_hash`` must equal the hash of the IR being
    signed, so a verdict for some *other* composition cannot be laundered onto
    this one;
  * the recorded codes are :func:`discharged_guarantees` — the numbered G-rules
    the SHIPPED frontend ruleset actually cites in its refusals, read out of the
    ruleset modules rather than out of the docs catalogue.

Honest spelling of what remains: an attestation says "the frontend identified
by ``checker`` ran over this exact composition and admitted it, and that
ruleset enforces these G-rules". It is NOT a proof of the G-rules themselves,
and a verifier that does not know the signer's ``checker.ruleset`` digest
learns which checker asserted it, not that the checker is any good.

Envelope validation and domain separation (v2)
----------------------------------------------
``verify_attestation`` validates the envelope after the MAC: ``kind``,
``version``, ``verdict``, ``sign_alg``, ``hash_alg`` and the shape of
``composition_hash`` / ``key_id`` / ``timestamp`` / ``guarantees`` / ``checker``
must be the expected values. A record whose ``sign_alg`` says ``ed25519`` is
REFUSED rather than quietly MAC-verified (the algorithm-confusion downgrade),
and a guarantee list carrying codes this catalogue does not define is refused
rather than printed as VALID. ``_sign`` prefixes a domain-separation tag, so an
attestation MAC and a ``revl.deploy`` receipt MAC live in different domains and
neither verifies as the other even under the same key.

Future work (not built here, and deliberately not stubbed with an unpinned
dependency): an **asymmetric** upgrade — Ed25519 signatures so a verifier needs
only a public key — would let untrusted parties verify without holding the
signing secret. The attestation envelope already carries a ``sign_alg`` member
for exactly that migration; today the only accepted value is
``"hmac-sha256"``, and it is checked, not merely recorded.

Public surface
--------------
``run_gate(paths=..., source=...)``           — run the frontend, get a verdict
``make_attestation(ir, key, *, verdict, ...)`` — build a signed attestation dict
``verify_attestation(att, key, ir=None)``      — ``(ok, reason)``
``canonical_hash(ir)``                         — the IR content hash
``discharged_guarantees()`` / ``ruleset_digest()`` / ``checker_identity()``
``load_attestation`` / ``load_key`` / ``resolve_key`` — IO helpers
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from .diagnostics import GUARANTEES
from .errors import RevlError

# The attestation envelope identity (mirrors interchange's kind/version idea:
# a self-identifying tag + a MAJOR.MINOR line, additive within a MAJOR).
ATTEST_KIND = "revl.attestation"
#: v2 (this fix). The signed body gained a `checker` member and its
#: `guarantees` member changed meaning from a catalogue constant to a
#: measurement, and the MAC gained a domain-separation prefix — so a v1 record
#: is not a v2 record with a field missing, it is a DIFFERENT claim. `verify`
#: refuses any other version rather than reading a v1 guarantee list as if it
#: had been measured; re-attest with the current toolchain.
ATTEST_VERSION = "2.0"

# The signature algorithm recorded in every attestation. One value today; the
# member exists so an asymmetric upgrade is an additive change, not a reshape.
# It is VALIDATED at verify time, not merely recorded: `_sign` is
# unconditionally HMAC-SHA256, so a record whose `sign_alg` claims something
# else is a mislabel and is refused (the algorithm-confusion downgrade —
# `deploy.admit` gates its cross-domain refusal on this trust question).
SIGN_ALG = "hmac-sha256"
HASH_ALG = "sha256"

#: The domain-separation prefix folded into every attestation MAC. Without it,
#: `hmac(key, canonical(body))` is the same construction `deploy.verify_receipt`
#: uses, so an admission receipt verified as an attestation and vice versa
#: (cross-protocol confusion). The tag carries the envelope version, so a v1
#: record cannot be replayed as a v2 one even by a key holder.
SIGN_DOMAIN = b"revl.attestation/v2\x00"

# The verdict an attestation records. A composition is only attestable once it
# is *admitted* — compiled clean AND free of open holes (a draft compiles but
# admission refuses it, docs/holes.md), so "admitted" is the single verdict.
VERDICT_ADMITTED = "admitted"

# The environment variable the signer secret is read from when no `--key` path
# is given. The value is the secret bytes themselves (UTF-8). A key FILE may be
# named instead via `REVL_ATTEST_KEY_FILE`. A secret is NEVER hardcoded here.
KEY_ENV = "REVL_ATTEST_KEY"
KEY_FILE_ENV = "REVL_ATTEST_KEY_FILE"

# An optional human/agent signer label, recorded verbatim in the attestation
# and covered by the signature. Purely descriptive — identity is proven by the
# key, not by this string.
SIGNER_ENV = "REVL_ATTEST_SIGNER"


def _canonical_bytes(obj) -> bytes:
    """The byte-stable canonical serialization used for both hashing and
    signing. Same spelling `formatter._canonical_ir` uses (sorted keys), with
    compact separators so the bytes are unambiguous and whitespace-free."""
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def canonical_hash(ir: dict) -> str:
    """The content hash of an admitted IR document — its stable identity.

    Deterministic: identical IR (regardless of source formatting, since the IR
    is post-lowering) always yields the same hex digest. This is the value the
    attestation binds a verdict to, and the value `--verify` recomputes to
    detect that the composition changed.
    """
    return hashlib.sha256(_canonical_bytes(ir)).hexdigest()


def key_id(key: bytes) -> str:
    """A non-secret fingerprint of the signing key, recorded in the attestation
    so a verifier can tell *which* key it needs without the key being present.

    A truncated SHA-256 of the key: one-way (the secret is not recoverable) and
    stable (the same key always fingerprints the same way)."""
    return hashlib.sha256(b"revl-attest-keyid\x00" + key).hexdigest()[:16]


def catalogued_guarantees() -> list[str]:
    """The numbered composition-level G-rules the DIAGNOSTICS CATALOGUE defines
    (DESIGN §4). This is the vocabulary — the set of codes a guarantee list may
    legally draw from — and nothing more: membership here says a code EXISTS,
    never that anything checked it. Only the numbered G-rules are universal: a
    named guarantee like `G-SECRET` (item 256) holds conditionally, for a
    composition that declares a secret, so it is a diagnostic code but not part
    of the invariant set an admitted verdict attests. Matching `G` followed by
    digits keeps it out."""
    return sorted(code for code in GUARANTEES
                  if code.startswith("G") and code[1:].isdigit())


#: The modules that MAKE UP the frontend ruleset — the code that actually
#: refuses a composition. `lower.py` is the checker proper; the rest are the
#: stages that raise a G-tagged refusal of their own (`parser`, `compiler`,
#: `admission`, `activation`, `taint`, `placement`, `emission_analysis`,
#: `admit_profile`), plus `holes` (the admission gate `run_gate` applies) and
#: `diagnostics` (the catalogue the codes are drawn from). Their bytes are what
#: :func:`ruleset_digest` identifies.
RULESET_MODULES = ("parser", "lower", "compiler", "admission", "activation",
                   "taint", "placement", "emission_analysis", "admit_profile",
                   "holes", "diagnostics")

#: The modules SCANNED for the G-codes the ruleset cites. `diagnostics` is
#: excluded on purpose: it is the catalogue, and reading the list off the
#: catalogue is exactly the constant this fix removes.
_CITING_MODULES = tuple(m for m in RULESET_MODULES if m != "diagnostics")

_G_TAG = re.compile(r"\(G([1-9])\)")

_ruleset_cache: Optional[tuple[str, tuple[str, ...]]] = None


def _read_ruleset() -> tuple[str, tuple[str, ...]]:
    """`(digest, cited codes)` for the frontend ruleset AS SHIPPED, computed
    once per process from the module sources on disk.

    The digest folds each module's name and bytes with explicit length framing,
    in a fixed order, so it is a stable identity of the implementation that did
    the checking — not of the documentation about it. The cited set is every
    `(Gn)` tag those modules carry; the tags are how the frontend names the rule
    a refusal is about, so a rule the shipped ruleset no longer cites drops out
    of the attested list.
    """
    global _ruleset_cache  # noqa: PLW0603 — a process-lifetime memo of on-disk bytes
    if _ruleset_cache is not None:
        return _ruleset_cache
    here = os.path.dirname(os.path.abspath(__file__))
    digest = hashlib.sha256(b"revl-attest-ruleset\x00")
    cited: set[str] = set()
    for name in RULESET_MODULES:
        path = os.path.join(here, f"{name}.py")
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as error:  # a ruleset we cannot read is not one we can name
            raise RevlError(
                path, 0,
                f"cannot read the frontend ruleset module {name!r}: {error}"
            ) from error
        label = name.encode("utf-8")
        digest.update(len(label).to_bytes(8, "big"))
        digest.update(label)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        if name in _CITING_MODULES:
            cited.update("G" + m.group(1)
                         for m in _G_TAG.finditer(data.decode("utf-8", "replace")))
    _ruleset_cache = (digest.hexdigest(), tuple(sorted(cited)))
    return _ruleset_cache


def ruleset_digest() -> str:
    """A sha256 identifying the frontend ruleset that produced a verdict, bound
    into the signed body so a verifier can tell WHICH checker asserted it.

    What it proves: two attestations carrying the same digest were signed by
    toolchains whose frontend ruleset bytes are identical. What it does NOT
    prove: that the ruleset is correct, or that the verifier has any way to
    resolve the digest to a source tree it can read. It is an identity, and the
    `alg`-style migration slot is its precedent — a value recorded so a future
    verifier can pin it."""
    return _read_ruleset()[0]


def discharged_guarantees() -> list[str]:
    """The G-codes an `admitted` verdict FROM THIS RULESET discharges.

    Derived, not declared: the intersection of the catalogue vocabulary
    (:func:`catalogued_guarantees`) with the codes the shipped ruleset modules
    actually cite (:func:`ruleset_digest`'s scan). Delete the G2 check from the
    frontend and G2 stops being attested, which is the drift the old
    `sorted(GUARANTEES)` constant could not see.

    Honest limits, so the field is not read as more than it is: the scan is
    LEXICAL — it sees that the shipped ruleset names a rule in its refusals, not
    that the rule is implemented correctly or that this composition exercised
    it. The load-bearing measurement is elsewhere: this list only ever reaches a
    signature through a :class:`GateVerdict` in which the reference frontend
    ADMITTED this exact composition hash, and a composition it refuses is never
    signed at all.
    """
    cited = set(_read_ruleset()[1])
    return sorted(code for code in catalogued_guarantees() if code in cited)


def compiler_version() -> str:
    """The revl toolchain version recorded as `checker.compiler`."""
    from .gate import _language_version  # noqa: PLC0415 — lazy, avoids an import cycle

    return _language_version()


def checker_identity() -> dict:
    """`{"compiler": ..., "ruleset": ...}` — the identity of the frontend that
    produced a verdict, folded into the signed body (item 127 F-fix #2). Two
    members on purpose: the human-legible release, and the digest that actually
    distinguishes two builds of it."""
    return {"compiler": compiler_version(), "ruleset": ruleset_digest()}


def _now_iso(now) -> str:
    """Normalize the `now` argument to an ISO-8601 UTC string. Accepts a
    `datetime` (naive treated as UTC), an ISO string (passed through), or None
    (current time). Kept explicit so `make_attestation` is deterministic when a
    caller pins `now` — the whole attestation is a pure function of (ir, key,
    now)."""
    if now is None:
        now = datetime.now(timezone.utc)
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc).isoformat()
    if isinstance(now, str):
        return now
    raise TypeError(f"now must be a datetime, ISO string, or None, not {type(now).__name__}")


def _parse_iso(value: str):
    """The recorded timestamp as a `datetime`, or None when it is not an
    ISO-8601 instant. A timestamp a verifier cannot read is a timestamp a
    freshness policy cannot enforce, so the envelope check refuses one."""
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


SIGNATURE_FIELD = "signature"


# ---------------------------------------------------------------------------
# the gate verdict — the measurement an attestation records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateVerdict:
    """What the reference frontend CONCLUDED about one specific composition.

    Produced only by :func:`run_gate`, which runs the real compiler and the real
    admission gate. `make_attestation` refuses to sign without one, so an
    attestation cannot exist for a composition nothing checked.

    `composition_hash` is the hash of the document that was admitted, AFTER any
    caller-supplied normalization — so the equality `make_attestation` enforces
    against the IR it is about to sign is a true "this is the artifact the gate
    ran over", not a coincidence of spelling.
    """

    admitted: bool
    composition_hash: Optional[str] = None
    guarantees: tuple[str, ...] = ()
    checker: Mapping[str, str] = field(default_factory=dict)
    reason: str = ""
    #: the compiled IR, when the source compiled at all (a draft with open holes
    #: compiles and is then refused admission, so this can be set on a refusal).
    ir: Optional[dict] = None
    #: the frontend's own diagnostic, so a caller can re-raise it verbatim
    #: rather than degrade it into a string.
    error: Optional[BaseException] = None


def run_gate(paths: Optional[Sequence[str]] = None, *, source: Optional[str] = None,
             filename: str = "<attest>", manifest: Optional[dict] = None,
             modules: Optional[dict] = None,
             normalize: Optional[Any] = None) -> GateVerdict:
    """Run the reference frontend over a composition and report what it decided.

    This is the MEASUREMENT an attestation records. It is the same gate `revl
    run` applies — `compile_files`/`compile_source` for the checker, then
    `holes.refuse_admission` for the admission gate a draft fails (docs/holes.md)
    — so `run_gate` can never admit what `revl` refuses.

    Never raises for a refusal: a refused composition comes back as
    `GateVerdict(admitted=False, ...)` carrying the frontend's own `error`, so a
    caller that wants the diagnostic verbatim re-raises `verdict.error` and a
    caller that only wants a verdict reads `admitted`.

    `normalize` is applied to the compiled document before hashing, for callers
    that attest a normalized spelling of the IR (`bundle._canonical_ir`,
    `registry._normalize_ir_for_attest`, `truc.reproduce._normalized_ir`). It
    must be a pure rewriting of the same composition; it exists so the verdict's
    hash is the hash of the document that will actually be signed.
    """
    from .compiler import compile_files, compile_source  # noqa: PLC0415 — lazy
    from .holes import refuse_admission  # noqa: PLC0415

    checker = checker_identity()
    document: Optional[dict] = None
    try:
        if paths is not None:
            document = compile_files(list(paths), manifest=manifest)
        elif source is not None:
            document = compile_source(source, filename, manifest=manifest,
                                      modules=modules)
        else:
            raise RevlError("<attest>", 0,
                            "run_gate needs `paths` (source files) or `source` "
                            "(source text): there is no gate run without a "
                            "composition to run it over")
        refuse_admission(document)
    except RevlError as error:
        return GateVerdict(False, checker=checker, ir=document, error=error,
                           reason=f"the reference frontend refused it: {error}")

    attested = normalize(document) if normalize is not None else document
    return GateVerdict(
        True, composition_hash=canonical_hash(attested),
        guarantees=tuple(discharged_guarantees()), checker=checker,
        ir=document,
        reason="the reference frontend compiled and admitted this composition")


def _body(ir_hash: str, timestamp: str, signer: str | None,
          kid: str, guarantees: Sequence[str], checker: Mapping[str, str],
          evidence_bindings: dict | None = None) -> dict:
    """The signed body of an attestation — every field EXCEPT the signature.
    `make_attestation` builds this, signs its canonical bytes, and appends the
    signature. `verify_attestation` signs over exactly the received members
    (everything but `signature`), so ANY altered, dropped, or added member
    breaks the signature — the whole record is committed, not a chosen subset.

    `evidence_bindings` (roadmap item 290, §6.2) is the per-facet sha256 of each
    evidence dossier the bundle publishes, folded INTO the signed payload so a
    forged or copied dossier is caught: `attestation valid` at admission means
    the signature verifies AND every bound dossier hashes to its signed value.
    The member is present only when bindings are supplied, so an attestation
    with no dossiers to bind carries no `evidence_bindings` member.

    `guarantees` and `checker` are the v2 members: the codes the supplied gate
    verdict discharged, and the identity of the frontend that produced it."""
    body = {
        "kind": ATTEST_KIND,
        "version": ATTEST_VERSION,
        "verdict": VERDICT_ADMITTED,
        "hash_alg": HASH_ALG,
        "composition_hash": ir_hash,
        "guarantees": list(guarantees),
        "checker": dict(sorted(checker.items())),
        "timestamp": timestamp,
        "sign_alg": SIGN_ALG,
        "signer": signer,
        "key_id": kid,
    }
    if evidence_bindings:
        # sorted so the signed bytes are a pure function of the bindings, not of
        # insertion order (the same discipline `_canonical_bytes` relies on).
        body["evidence_bindings"] = dict(sorted(evidence_bindings.items()))
    return body


def _sign(body: dict, key: bytes) -> str:
    """The HMAC-SHA256 signature over the canonical body bytes, hex. `body` is
    the attestation with its `signature` member removed; canonical serialization
    sorts keys, so member order does not affect the signature.

    The MAC is taken over :data:`SIGN_DOMAIN` ++ the canonical bytes. The prefix
    is not decoration: `deploy.verify_receipt` MACs an admission receipt with
    the same key material and the same canonical serialization, so WITHOUT a
    domain tag an ACCEPT receipt verified as a valid attestation and an
    attestation verified as a valid receipt. Two protocols, two domains."""
    signed = {k: v for k, v in body.items() if k != SIGNATURE_FIELD}
    return hmac.new(key, SIGN_DOMAIN + _canonical_bytes(signed),
                    hashlib.sha256).hexdigest()


def make_attestation(ir: dict, key: bytes, *,
                     verdict: Optional[GateVerdict] = None, now=None,
                     signer: str | None = None,
                     evidence_bindings: dict | None = None) -> dict:
    """Build a signed attestation for an admitted composition IR.

    `verdict` is REQUIRED and must come from :func:`run_gate`: an attestation
    records a measurement, so there is nothing to sign until the reference
    frontend has actually run. Three refusals, all `RevlError`:

      * no verdict, or one the gate did not admit — a composition the compiler
        refuses is never signed, so the G2-violating composition that used to
        carry a signature asserting G2 now carries none;
      * a verdict whose `composition_hash` is not the hash of `ir` — a real
        verdict for a DIFFERENT composition cannot be laundered onto this one;
      * a verdict whose guarantee codes are not codes this catalogue defines.

    Pure and deterministic given `now`: the same (ir, key, verdict, now, signer,
    evidence_bindings) always produces byte-identical output, which is what
    makes the round-trip testable and the attestation reproducible.

    `evidence_bindings` (item 290, §6.2) binds the per-facet dossier hashes into
    the signed payload.
    """
    if not isinstance(key, (bytes, bytearray)) or not key:
        raise RevlError("<attest>", 0, "signing key must be non-empty bytes")
    if not isinstance(verdict, GateVerdict):
        raise RevlError(
            "<attest>", 0,
            "attesting needs a gate verdict from `attest.run_gate`: an "
            "attestation records what the reference frontend MEASURED about "
            "this composition, and there is nothing to sign without a run")
    if not verdict.admitted:
        raise RevlError(
            "<attest>", 0,
            "the gate did not admit this composition, so there is no admitted "
            f"verdict to attest — {verdict.reason}")
    holes = ir.get("holes") or []
    if holes:
        raise RevlError(
            "<attest>", 0,
            f"composition has {len(holes)} open hole(s) — it is a draft, not "
            f"admitted, and cannot be attested (docs/holes.md)")

    ir_hash = canonical_hash(ir)
    if not hmac.compare_digest(ir_hash, str(verdict.composition_hash)):
        raise RevlError(
            "<attest>", 0,
            "the gate verdict is for a different composition: the gate admitted "
            f"{str(verdict.composition_hash)[:12]}…, the IR being attested "
            f"hashes to {ir_hash[:12]}…. A verdict is only evidence about the "
            "artifact it ran over.")
    guarantees = sorted(set(verdict.guarantees))
    unknown = [code for code in guarantees if code not in catalogued_guarantees()]
    if unknown or not guarantees:
        raise RevlError(
            "<attest>", 0,
            "the gate verdict records no usable guarantee codes "
            f"({', '.join(unknown) or '(empty)'} is not in the composition "
            "guarantee catalogue)")

    kid = key_id(bytes(key))
    body = _body(ir_hash, _now_iso(now), signer, kid, guarantees,
                 verdict.checker, evidence_bindings)
    signature = _sign(body, bytes(key))
    return {**body, SIGNATURE_FIELD: signature}


_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_HEX16 = re.compile(r"\A[0-9a-f]{16}\Z")


def _validate_envelope(att: Mapping) -> str:
    """The envelope check: is this record even an attestation of the shape this
    verifier accepts? Returns a refusal reason, or `""` when the envelope is
    well formed.

    A MAC proves authorship. It does NOT prove that what was authored means what
    the reader assumes, and a key holder authors freely — so every member whose
    value carries a fixed meaning is checked against that meaning here:

      * `kind`/`version` — a `revl.deploy.receipt`, or a v1 attestation whose
        guarantee list was never measured, is not a v2 attestation;
      * `verdict` — `admitted` is the only verdict an attestation records, so a
        record reading `REJECTED-by-the-gauntlet` is refused, not printed VALID;
      * `sign_alg` — `_sign` is unconditionally HMAC-SHA256, so a record
        labelled `ed25519` is a MISLABEL, and one trusted downstream (deploy's
        cross-domain refusal) reads exactly this field;
      * `guarantees` — codes must come from the composition guarantee
        catalogue, so `G10-does-not-exist`/`SOC2`/`FIPS-140-3` cannot ride along;
      * `checker` — the signer's frontend identity must be present and shaped,
        so "which checker asserted this" always has an answer.

    The guarantee codes are NOT required to equal this verifier's own
    `discharged_guarantees()`: a signer on a different toolchain legitimately
    discharges a different set, and `checker.ruleset` is how a verifier decides
    whether it accepts that signer's ruleset. Vocabulary is checked here;
    ruleset policy belongs to the caller.
    """
    def _reason(member, expected, found):
        return (f"envelope refused: {member} is {found!r}, expected "
                f"{expected!r}")

    for member, expected in (("kind", ATTEST_KIND),
                             ("version", ATTEST_VERSION),
                             ("verdict", VERDICT_ADMITTED),
                             ("sign_alg", SIGN_ALG),
                             ("hash_alg", HASH_ALG)):
        found = att.get(member)
        if found != expected:
            return _reason(member, expected, found)

    if not _HEX64.match(str(att.get("composition_hash"))):
        return ("envelope refused: composition_hash is not a sha256 hex digest "
                f"({att.get('composition_hash')!r})")
    if not _HEX16.match(str(att.get("key_id"))):
        return f"envelope refused: key_id is not a key fingerprint ({att.get('key_id')!r})"
    timestamp = att.get("timestamp")
    if not isinstance(timestamp, str) or _parse_iso(timestamp) is None:
        return f"envelope refused: timestamp is not an ISO-8601 instant ({timestamp!r})"

    guarantees = att.get("guarantees")
    known = catalogued_guarantees()
    if (not isinstance(guarantees, list) or not guarantees
            or not all(isinstance(code, str) for code in guarantees)):
        return f"envelope refused: guarantees is not a non-empty list of codes ({guarantees!r})"
    unknown = [code for code in guarantees if code not in known]
    if unknown:
        return ("envelope refused: guarantees names "
                f"{', '.join(repr(code) for code in unknown)}, which the "
                f"composition guarantee catalogue does not define "
                f"(known: {', '.join(known)})")
    if list(guarantees) != sorted(set(guarantees)):
        return ("envelope refused: guarantees must be sorted and free of "
                f"duplicates ({guarantees!r})")

    checker = att.get("checker")
    if not isinstance(checker, dict):
        return ("envelope refused: no `checker` member, so nothing says WHICH "
                "frontend asserted this")
    if not isinstance(checker.get("compiler"), str) or not checker["compiler"]:
        return f"envelope refused: checker.compiler is not a version ({checker.get('compiler')!r})"
    if not _HEX64.match(str(checker.get("ruleset"))):
        return ("envelope refused: checker.ruleset is not a sha256 ruleset "
                f"digest ({checker.get('ruleset')!r})")

    signer = att.get("signer")
    if signer is not None and not isinstance(signer, str):
        return f"envelope refused: signer is not a label ({signer!r})"

    bindings = att.get("evidence_bindings")
    if bindings is not None:
        if not isinstance(bindings, dict):
            return f"envelope refused: evidence_bindings is not an object ({bindings!r})"
        for facet, digest in sorted(bindings.items()):
            if not isinstance(facet, str) or not _HEX64.match(str(digest)):
                return ("envelope refused: evidence_bindings["
                        f"{facet!r}] is not a sha256 digest ({digest!r})")
    return ""


def verify_attestation(att: dict, key: bytes, ir: dict | None = None
                       ) -> tuple[bool, str]:
    """Check an attestation with `key`. Returns `(ok, reason)`.

    Three independent failure modes, reported distinctly:

      * **signature mismatch** — the HMAC over the payload does not match. The
        key is wrong, or a field of the attestation (verdict, timestamp, hash,
        guarantees, signer) was altered after signing. Checked first: it proves
        the attestation itself is authentic and untampered before its contents
        are trusted. The MAC is domain-separated, so a `revl.deploy` receipt
        signed with this key fails here rather than passing as an attestation.
      * **envelope refused** — the record is authentic but is not an attestation
        of the shape this verifier accepts: a mislabelled `sign_alg`, a verdict
        other than `admitted`, a guarantee list naming codes the catalogue does
        not define, a missing `checker`, or a version whose members do not mean
        what v2's mean. Authenticity is not authority: a key holder can author a
        well-signed record that says anything, so what it says is checked too.
      * **hash mismatch** — only when `ir` is supplied: the attestation is
        authentic and well formed, but the composition presented now hashes
        differently from the one it was signed for. The composition changed.

    A missing/short `key`, or an attestation missing required members, is a
    signature-level failure — never silently "valid".
    """
    if not isinstance(key, (bytes, bytearray)) or not key:
        return False, "no signing key provided"
    if not isinstance(att, dict):
        return False, "attestation is not an object"

    given_sig = att.get(SIGNATURE_FIELD)
    if not isinstance(given_sig, str):
        return False, "attestation has no signature"
    if "composition_hash" not in att:
        return False, "attestation missing required member 'composition_hash'"

    # Sign over exactly the received members (everything but `signature`). Any
    # altered, dropped, or added member — verdict, timestamp, hash, guarantees,
    # signer, or an injected field — makes the recomputed HMAC diverge, so all
    # tampering is caught here as a signature mismatch.
    expected_sig = _sign(att, bytes(key))
    if not hmac.compare_digest(expected_sig, given_sig):
        return False, ("signature mismatch: wrong key, or the attestation was "
                       "tampered with after signing")

    # The record is authentic. Now: is it an attestation, and does it claim
    # only what an attestation is allowed to claim? A key holder signs freely,
    # so this check is what stops a well-signed record from meaning anything it
    # likes (F1/F9, and it makes a forged guarantee list non-verifiable).
    envelope = _validate_envelope(att)
    if envelope:
        return False, envelope

    if ir is not None:
        recomputed = canonical_hash(ir)
        if not hmac.compare_digest(recomputed, att["composition_hash"]):
            return False, ("hash mismatch: the composition changed since it was "
                           "attested (attested "
                           f"{att['composition_hash'][:12]}…, now "
                           f"{recomputed[:12]}…)")

    return True, "valid: attestation is authentic and the composition matches"


# --- IO helpers -----------------------------------------------------------

def load_attestation(path: str) -> dict:
    """Load an attestation JSON document from disk."""
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RevlError(path, 0, f"cannot read attestation: {error}") from error
    if not isinstance(doc, dict):
        raise RevlError(path, 0, "attestation is not a JSON object")
    return doc


def load_key(path: str) -> bytes:
    """Read a signing key from a file, as raw bytes.

    A trailing newline is stripped (so a key written with `echo` or an editor
    round-trips), but the key is otherwise used verbatim — no encoding
    assumptions."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as error:
        raise RevlError(path, 0, f"cannot read key file: {error}") from error
    if data.endswith(b"\n"):
        data = data[:-1]
    if not data:
        raise RevlError(path, 0, "key file is empty")
    return data


def resolve_key(key_path: str | None, *, env=None) -> bytes:
    """Resolve the signing key from, in order: an explicit `--key` path, the
    `REVL_ATTEST_KEY_FILE` env var (a path), then `REVL_ATTEST_KEY` (the secret
    bytes directly). Never a hardcoded default — a missing key is an error, so a
    secret is never committed or assumed.
    """
    if env is None:
        import os  # noqa: PLC0415 — lazy so the module has no import side effect
        env = os.environ
    if key_path:
        return load_key(key_path)
    file_env = env.get(KEY_FILE_ENV)
    if file_env:
        return load_key(file_env)
    inline = env.get(KEY_ENV)
    if inline:
        return inline.encode("utf-8")
    raise RevlError(
        "<attest>", 0,
        f"no signing key: pass --key PATH, or set {KEY_FILE_ENV} (a key file) "
        f"or {KEY_ENV} (the secret)")


def render_attestation(att: dict) -> str:
    """Human-readable form of an attestation (the default `revl attest`
    output; the full record is under `--json`)."""
    lines = [
        f"attestation: {att.get('verdict', '?')}  "
        f"({att.get('kind')} v{att.get('version')})",
        f"  composition {att.get('hash_alg', '?')}: "
        f"{att.get('composition_hash', '?')}",
        f"  guarantees: {', '.join(att.get('guarantees') or [])}",
        f"  checker:    revl {(att.get('checker') or {}).get('compiler', '?')}"
        f", ruleset {str((att.get('checker') or {}).get('ruleset', '?'))[:12]}…",
        f"  signed:     {att.get('timestamp', '?')}  "
        f"({att.get('sign_alg', '?')}, key {att.get('key_id', '?')})",
    ]
    if att.get("signer"):
        lines.append(f"  signer:     {att['signer']}")
    lines.append(f"  signature:  {att.get('signature', '?')}")
    return "\n".join(lines)


def render_verify(ok: bool, reason: str, att: dict) -> str:
    """Human-readable form of a `--verify` result."""
    glyph = "VALID" if ok else "INVALID"
    head = (f"attestation: {glyph} — {reason}")
    detail = (f"  composition {att.get('hash_alg', '?')}: "
              f"{att.get('composition_hash', '?')}")
    return f"{head}\n{detail}"

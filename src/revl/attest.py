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

Future work (not built here, and deliberately not stubbed with an unpinned
dependency): an **asymmetric** upgrade — Ed25519 signatures so a verifier needs
only a public key — would let untrusted parties verify without holding the
signing secret. The attestation envelope already carries an ``alg`` member for
exactly that migration; today the only value is ``"hmac-sha256"``.

Public surface
--------------
``make_attestation(ir, key, *, now=...)``  — build a signed attestation dict
``verify_attestation(att, key, ir=None)``   — ``(ok, reason)``
``canonical_hash(ir)``                        — the IR content hash
``load_attestation`` / ``load_key`` / ``resolve_key`` — IO helpers
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

from .diagnostics import GUARANTEES
from .errors import RevlError

# The attestation envelope identity (mirrors interchange's kind/version idea:
# a self-identifying tag + a MAJOR.MINOR line, additive within a MAJOR).
ATTEST_KIND = "revl.attestation"
ATTEST_VERSION = "1.0"

# The signature algorithm recorded in every attestation. One value today; the
# member exists so an asymmetric upgrade is an additive change, not a reshape.
SIGN_ALG = "hmac-sha256"
HASH_ALG = "sha256"

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


def attested_guarantees() -> list[str]:
    """The guarantee codes an `admitted` verdict proves — the numbered
    composition-level G-rules G1..G9 (DESIGN §4), drawn live from
    `diagnostics.GUARANTEES` so this list cannot drift from the catalog. The
    lifecycle A-rules and type T-rules are checked too, but the G-codes are the
    boundary/composition guarantees an attestation is about, so those are what it
    records. Only the numbered G-rules are universal: a named guarantee like
    `G-SECRET` (item 256) holds conditionally, for a composition that declares a
    secret, so it is a diagnostic code but not part of the invariant set every
    admitted verdict attests. Matching `G` followed by digits keeps it out."""
    return sorted(code for code in GUARANTEES
                  if code.startswith("G") and code[1:].isdigit())


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


SIGNATURE_FIELD = "signature"


def _body(ir_hash: str, timestamp: str, signer: str | None,
          kid: str, evidence_bindings: dict | None = None) -> dict:
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
    with no dossiers to bind is byte-identical to the pre-290 record."""
    body = {
        "kind": ATTEST_KIND,
        "version": ATTEST_VERSION,
        "verdict": VERDICT_ADMITTED,
        "hash_alg": HASH_ALG,
        "composition_hash": ir_hash,
        "guarantees": attested_guarantees(),
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
    sorts keys, so member order does not affect the signature."""
    signed = {k: v for k, v in body.items() if k != SIGNATURE_FIELD}
    return hmac.new(key, _canonical_bytes(signed), hashlib.sha256).hexdigest()


def make_attestation(ir: dict, key: bytes, *, now=None,
                     signer: str | None = None,
                     evidence_bindings: dict | None = None) -> dict:
    """Build a signed attestation for an admitted composition IR.

    Pure and deterministic given `now`: the same (ir, key, now, signer,
    evidence_bindings) always produces byte-identical output, which is what
    makes the round-trip testable and the attestation reproducible.

    `evidence_bindings` (item 290, §6.2) binds the per-facet dossier hashes into
    the signed payload; omit it and the record is byte-identical to the pre-290
    attestation. Raises `RevlError` if the IR is not attestable — a draft with
    open holes compiles but is NOT admitted (docs/holes.md), so attesting it
    would sign a false verdict.
    """
    if not isinstance(key, (bytes, bytearray)) or not key:
        raise RevlError("<attest>", 0, "signing key must be non-empty bytes")
    holes = ir.get("holes") or []
    if holes:
        raise RevlError(
            "<attest>", 0,
            f"composition has {len(holes)} open hole(s) — it is a draft, not "
            f"admitted, and cannot be attested (docs/holes.md)")

    ir_hash = canonical_hash(ir)
    kid = key_id(bytes(key))
    body = _body(ir_hash, _now_iso(now), signer, kid, evidence_bindings)
    signature = _sign(body, bytes(key))
    return {**body, SIGNATURE_FIELD: signature}


def verify_attestation(att: dict, key: bytes, ir: dict | None = None
                       ) -> tuple[bool, str]:
    """Check an attestation with `key`. Returns `(ok, reason)`.

    Two independent failure modes, reported distinctly:

      * **signature mismatch** — the HMAC over the payload does not match. The
        key is wrong, or a field of the attestation (verdict, timestamp, hash,
        guarantees, signer) was altered after signing. Checked first: it proves
        the attestation itself is authentic and untampered before its contents
        are trusted.
      * **hash mismatch** — only when `ir` is supplied: the attestation is
        authentic, but the composition presented now hashes differently from
        the one it was signed for. The composition changed.

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

"""Chain-neutral anchor records over existing digests (issue #2723).

revl already signs digests of the things an operator may want to prove later:
`attest.canonical_hash` identifies an admitted composition, `revl attest` signs
that identity into an attestation, and the peer pool's receipts carry a
`receipt_digest`. None of those records is a value an operator can hand to an
outside system on its own, because each one is about one artifact.

This module defines a small record that commits to a SET of those digests and
signs it under the existing attestation key. The operator publishes the
record's `anchor_hash` wherever it likes: a transparency log, a timestamping
service, a ledger. revl never contacts any of them. The external system is the
operator's, and it is untrusted: an anchor says a set of digests existed and
was signed, never that the code behind the digests is correct.

Two claims are deliberately absent:

  * an anchor is not an attestation. It carries no verdict, no guarantee codes
    and no checker identity, so it cannot be read as evidence that a
    composition was admitted. `revl attest` remains the record that says that;
  * an anchor is not a timestamp authority. Its `timestamp` is when the
    signer says it signed, inside the MAC, so it cannot be edited afterwards,
    but proving WHEN a value existed is the external system's job.

Record, kind ``revl.anchor`` version ``1.0``::

    {kind, version, hash_alg, digests, anchor_hash, timestamp, sign_alg,
     signer, key_id, signature}

`digests` is a JSON object mapping a name to one sha256 digest. `anchor_hash`
is sha256 over its canonical spelling (sorted keys, compact separators, UTF-8),
so the published value is recomputable from the record by any verifier::

    sha256(json.dumps(digests, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8"))

`signature` is HMAC-SHA256 over the canonical spelling of every other member,
prefixed with :data:`ANCHOR_SIGN_DOMAIN`. The distinct domain is what keeps an
anchor from verifying as an attestation, a certificate, a deploy receipt or a
pool receipt under the same key.

Versioning is an envelope property: a verifier refuses a version it does not
implement rather than reading an unknown shape as if it were this one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from typing import Any

from . import attest
from .errors import RevlError

#: The anchor envelope identity. The version is additive within a major line;
#: a verifier refuses any value it does not implement.
ANCHOR_KIND = "revl.anchor"
ANCHOR_VERSION = "1.0"

#: Domain separation for the anchor MAC. One key signs attestations,
#: certificates, deploy receipts and pool receipts already, and each protocol
#: carries its own tag so none of them verifies as another.
ANCHOR_SIGN_DOMAIN = b"revl.anchor/v1\x00"

#: The only hash the digests in an anchor may carry. Existing revl digests
#: (the composition hash, the attestation MAC input, a pool receipt digest)
#: are sha256, and one spelling keeps the record checkable without an
#: algorithm-negotiation surface.
_HASH_NAME = "sha256"

#: A digest name is an operator-facing key, so it is kept printable and
#: bounded. The colon and slash are allowed so names like `receipt:peer-1` and
#: `attestation/ci` read naturally.
_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}\Z")
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_HEX16_RE = re.compile(r"\A[0-9a-f]{16}\Z")


def anchor_hash(digests: Mapping[str, str]) -> str:
    """sha256 over the canonical spelling of the named digests.

    Defined here and nowhere else, so the builder and the verifier cannot
    disagree about what the published value commits to. The caller is
    responsible for the shape; :func:`_validated_digests` is the refusing
    boundary used when a record is built.
    """
    return hashlib.sha256(attest._canonical_bytes(dict(digests))).hexdigest()


def _validated_digests(digests: Any) -> dict[str, str]:
    """Check the digest set and return it sorted, ready to be signed.

    Refusals are `RevlError`: an anchor with no digests commits to nothing, a
    name outside the documented shape cannot be addressed by a verifier, and a
    value that is not one lowercase sha256 digest cannot be compared with the
    artifact it names.
    """
    if not isinstance(digests, Mapping):
        raise RevlError("<anchor>", 0,
                        "anchor digests must be a mapping of name to sha256 "
                        f"digest, not {type(digests).__name__}")
    if not digests:
        raise RevlError("<anchor>", 0,
                        "an anchor commits to at least one digest; it cannot "
                        "be built from an empty set")
    checked: dict[str, str] = {}
    for name, value in digests.items():
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise RevlError(
                "<anchor>", 0,
                f"digest name {name!r} is not addressable: a name is 1 to 64 "
                "characters of A-Z, a-z, 0-9, '.', '_', ':', '/' or '-', "
                "starting with a letter or digit")
        if not isinstance(value, str) or not _DIGEST_RE.match(value):
            raise RevlError(
                "<anchor>", 0,
                f"digest {name!r} is not a sha256 hex digest ({value!r}); the "
                "value is 64 lowercase hex characters")
        checked[name] = value
    return dict(sorted(checked.items()))


def parse_digest_arg(text: str) -> tuple[str, str]:
    """Read one `NAME=DIGEST` command-line argument.

    One parser for both `revl attest --anchor --digest` and `revl verify
    --anchor --digest`, so the emitter and the verifier accept exactly the same
    spelling and a value accepted by one is never refused by the other.
    """
    name, separator, value = text.partition("=")
    if not separator:
        raise RevlError("<anchor>", 0,
                        f"expected NAME=DIGEST, got {text!r}")
    name, value = name.strip(), value.strip()
    if not _NAME_RE.match(name):
        raise RevlError(
            "<anchor>", 0,
            f"digest name {name!r} is not addressable: a name is 1 to 64 "
            "characters of A-Z, a-z, 0-9, '.', '_', ':', '/' or '-', "
            "starting with a letter or digit")
    if not _DIGEST_RE.match(value):
        raise RevlError(
            "<anchor>", 0,
            f"digest for {name!r} is not a sha256 hex digest ({value!r}); the "
            "value is 64 lowercase hex characters")
    return name, value


def _body(digests: dict[str, str], timestamp: str, signer: str | None,
          kid: str, committed: str) -> dict:
    """The signed body of an anchor: every member except the signature."""
    return {
        "kind": ANCHOR_KIND,
        "version": ANCHOR_VERSION,
        "hash_alg": _HASH_NAME,
        "digests": dict(digests),
        "anchor_hash": committed,
        "timestamp": timestamp,
        "sign_alg": attest.SIGN_ALG,
        "signer": signer,
        "key_id": kid,
    }


def _sign(body: dict, key: bytes) -> str:
    """HMAC-SHA256 over the anchor's canonical body bytes, hex.

    Mirrors `attest._sign` and `cert._sign`, with this protocol's own domain
    tag. The tag is folded in before the canonical bytes, so an attestation or
    receipt signed under the same key does not verify as an anchor.
    """
    signed = {member: value for member, value in body.items()
              if member != attest.SIGNATURE_FIELD}
    return hmac.new(key, ANCHOR_SIGN_DOMAIN + attest._canonical_bytes(signed),
                    hashlib.sha256).hexdigest()


def make_anchor(digests: Mapping[str, str], key: bytes, *, now=None,
                signer: str | None = None) -> dict:
    """Build a signed anchor over `digests`.

    Pure and deterministic given `now`: the same (digests, key, now, signer)
    produces byte-identical output. `digests` is name -> sha256 hex; an empty
    set, an unaddressable name or a non-sha256 value is a `RevlError`.
    """
    if not isinstance(key, (bytes, bytearray)) or not key:
        raise RevlError("<anchor>", 0, "signing key must be non-empty bytes")
    if signer is not None and not isinstance(signer, str):
        raise RevlError("<anchor>", 0,
                        f"signer label must be a string, not "
                        f"{type(signer).__name__}")
    checked = _validated_digests(digests)
    body = _body(checked, attest._now_iso(now), signer,
                 attest.key_id(bytes(key)), anchor_hash(checked))
    return {**body, attest.SIGNATURE_FIELD: _sign(body, bytes(key))}


def _validate_envelope(anchor: Mapping) -> str:
    """Is this record an anchor of the shape this build implements?

    Returns a refusal reason or `""`. Authenticity is checked before this, so
    every value here has already been proven to come from a key holder; the
    check answers whether what a key holder wrote means what a reader assumes.
    """
    version = anchor.get("version")
    if version != ANCHOR_VERSION:
        return (f"envelope refused: unsupported anchor version {version!r} "
                f"(this build implements {ANCHOR_VERSION!r})")
    for member, expected in (("kind", ANCHOR_KIND),
                             ("hash_alg", _HASH_NAME),
                             ("sign_alg", attest.SIGN_ALG)):
        found = anchor.get(member)
        if found != expected:
            return (f"envelope refused: {member} is {found!r}, expected "
                    f"{expected!r}")

    digests = anchor.get("digests")
    if not isinstance(digests, dict) or not digests:
        return ("envelope refused: digests is not a non-empty object of name "
                f"to sha256 digest ({digests!r})")
    for name, value in digests.items():
        if not isinstance(name, str) or not _NAME_RE.match(name):
            return (f"envelope refused: digest name {name!r} is not "
                    "addressable")
        if not isinstance(value, str) or not _DIGEST_RE.match(value):
            return (f"envelope refused: digest {name!r} is not a sha256 hex "
                    f"digest ({value!r})")
    committed = anchor.get("anchor_hash")
    if not isinstance(committed, str) or not _DIGEST_RE.match(committed):
        return ("envelope refused: anchor_hash is not a sha256 hex digest "
                f"({committed!r})")
    timestamp = anchor.get("timestamp")
    if not isinstance(timestamp, str) or attest._parse_iso(timestamp) is None:
        return (f"envelope refused: timestamp is not an ISO-8601 instant "
                f"({timestamp!r})")
    kid = anchor.get("key_id")
    if not isinstance(kid, str) or not _HEX16_RE.match(kid):
        return f"envelope refused: key_id is not a key fingerprint ({kid!r})"
    signer = anchor.get("signer")
    if signer is not None and not isinstance(signer, str):
        return f"envelope refused: signer is not a label ({signer!r})"
    return ""


def verify_anchor(anchor: dict, key: bytes, *,
                  expected: Mapping[str, str] | None = None
                  ) -> tuple[bool, str]:
    """Check an anchor with `key`. Returns `(ok, reason)`. Never raises.

    The checks are independent and reported distinctly:

      * **signature mismatch**: the HMAC does not match. The key is wrong, a
        member was altered after signing, or the record belongs to another
        protocol domain (an attestation, a certificate, a receipt). Checked
        first, because it proves the record is authentic before its contents
        are read.
      * **identity mismatch**: the MAC verifies, but `key_id` names a
        different key. The fingerprint is a function of the key alone, so a
        record that names another one is about another signer even when the
        MAC is right.
      * **envelope refused**: authentic, but not an anchor this build
        implements. An unknown version is refused here rather than read as
        version 1.0, and the same goes for a mislabelled algorithm, a missing
        or malformed digest, and an unreadable timestamp.
      * **anchor hash mismatch**: authentic and well formed, but `anchor_hash`
        is not the sha256 of the digests it claims to commit to. A key holder
        can sign a false commitment, so the value is recomputed rather than
        read.
      * **digest mismatch** *(only with `expected`)*: the anchor does not
        commit to one of the digests the verifier holds, or commits to a
        different value for that name. This is the check that ties the record
        to artifacts in the verifier's hands; the `--digest` option on `revl
        verify --anchor` supplies it.
    """
    if not isinstance(key, (bytes, bytearray)) or not key:
        return False, "no signing key provided"
    if not isinstance(anchor, dict):
        return False, "anchor is not an object"

    given_sig = anchor.get(attest.SIGNATURE_FIELD)
    if not isinstance(given_sig, str):
        return False, "anchor has no signature"
    not_ascii = attest.signature_not_ascii_reason(given_sig)
    if not_ascii:
        return False, not_ascii

    try:
        expected_sig = _sign(anchor, bytes(key))
    except attest.NotCanonicalizable as error:
        return False, f"anchor cannot be verified: {error}"
    if not hmac.compare_digest(expected_sig, given_sig):
        return False, ("signature mismatch: wrong key, or the anchor was "
                       "tampered with after signing")

    # The fingerprint is part of the signed body and a function of the key
    # alone, so a record naming a different one is a record about another
    # signer even when its MAC is right: `key_id` is what a reader uses to
    # decide WHICH key to fetch, and a MAC that verifies under a key the
    # record does not name is the one arrangement in which that decision
    # cannot be made.
    named = anchor.get("key_id")
    expected_kid = attest.key_id(bytes(key))
    if named != expected_kid:
        return False, ("identity mismatch: the anchor names key "
                       f"{str(named)[:64]!r}, which is not the key it is "
                       f"being checked with (this key is {expected_kid})")

    envelope = _validate_envelope(anchor)
    if envelope:
        return False, envelope

    try:
        committed = anchor_hash(anchor["digests"])
    except attest.NotCanonicalizable as error:
        return False, f"the anchor's digest set cannot be hashed: {error}"
    if not hmac.compare_digest(committed, anchor["anchor_hash"]):
        return False, ("anchor hash mismatch: the digest set does not hash to "
                       "the committed anchor_hash")

    if expected is not None:
        if not isinstance(expected, Mapping):
            return False, "expected digests are not a mapping of name to digest"
        try:
            names = sorted(expected)
        except TypeError:
            return False, "expected digest names are not comparable strings"
        for name in names:
            value = expected[name]
            if not isinstance(name, str) or not _NAME_RE.match(name):
                return False, f"expected digest name {name!r} is not addressable"
            if not isinstance(value, str) or not _DIGEST_RE.match(value):
                return False, (f"expected digest for {name!r} is not a sha256 "
                               "hex digest")
            if name not in anchor["digests"]:
                return False, (f"digest mismatch: the anchor commits to no "
                               f"{name!r} digest")
            if not hmac.compare_digest(anchor["digests"][name], value):
                return False, (f"digest mismatch for {name!r}: the anchor "
                               "commits to a different value")

    return True, "valid: anchor is authentic and commits to the digests presented"


def load_anchor(path: str) -> dict:
    """Load an anchor JSON document from disk.

    A document that is not a JSON object is refused here rather than at
    verification, so no caller has to decide what a list means.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RevlError(path, 0, f"cannot read anchor: {error}") from error
    if not isinstance(document, dict):
        raise RevlError(path, 0, "anchor is not a JSON object")
    return document


def render_anchor(anchor: dict) -> str:
    """Human-readable form of an anchor. The published value is `anchor
    sha256`; `--json` carries the whole record an operator ships."""
    lines = [
        f"anchor: {anchor.get('kind', '?')} v{anchor.get('version', '?')}",
        f"  anchor sha256: {anchor.get('anchor_hash', '?')}",
        "  digests:",
    ]
    for name, value in sorted((anchor.get("digests") or {}).items()):
        lines.append(f"    {name}  {value}")
    lines.append(f"  signed: {anchor.get('timestamp', '?')}  "
                 f"({anchor.get('sign_alg', '?')}, "
                 f"key {anchor.get('key_id', '?')})")
    if anchor.get("signer"):
        lines.append(f"  signer: {anchor['signer']}")
    lines.append(f"  signature: {anchor.get('signature', '?')}")
    return "\n".join(lines)


def render_verify(ok: bool, reason: str, anchor: dict) -> str:
    """Human-readable form of an anchor verification result."""
    headline = "VALID" if ok else "INVALID"
    return (f"anchor: {headline}: {reason}\n"
            f"  anchor sha256: {anchor.get('anchor_hash', '?')}")

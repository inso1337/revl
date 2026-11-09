"""Verify a release manifest signature, and require one (docs/updates.md).

The manifest is signed with the same primitive a peer identity uses:
:func:`revl.peer_identity.sign_record` covers every member of the document
except ``signature``, including the ``key_id`` that selects the verification
key. This module is the verifier half only. It holds no private key, reads no
network and stages nothing; the release workflow signs with
``tools/release_signing.py sign`` and the updater decides here whether to
believe the result.

Trust is a pin, not a promise
-----------------------------
:data:`release_keys.RELEASE_KEYS` is the only source of public keys. The
document's ``public_key`` member is passed through the record but never chosen
by the verifier: :func:`revl.peer_identity.verify_record` compares it against
the pinned key and refuses a record that names another. A manifest cannot
introduce the key that verifies it.

Both a ``key_id`` and a validity window are checked before any arithmetic
runs. An unknown key, a key used before ``notBefore`` or after ``notAfter``,
a malformed pinned entry and a malformed timestamp are all refusals with a
sentence, never exceptions from a hostile document.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from ..peer_identity import verify_record
from . import release_keys
from .errors import UpdateError

#: The signature domain. A release manifest and a peer record can never be
#: confused even if their bodies happen to look alike, because the domain is
#: signed in front of the canonical bytes.
RELEASE_DOMAIN = b"revl.release/1"

#: What a caller does about an unsigned release when the operator has not
#: opted into trusting one.
UNSIGNED_FIX = ("publish a signed release.json, or start the operator surface "
                "with --update-trust-unsigned to accept an unsigned release")


class _Refused(Exception):
    """An internal refusal carrying the sentence the caller sees. Kept local:
    :func:`verify_manifest` is the public surface and returns tuples."""


def _parse_instant(value: Any, *, where: str) -> datetime:
    """One ISO 8601 instant, in UTC, or a refusal naming the field.

    ``Z`` and ``+00:00`` both spell UTC and both are accepted. A naive string
    is read as UTC, matching ``peer_identity``'s treatment of its own
    timestamps; an unparseable or non-string value is a refusal, because
    failing open on a timestamp is how an expired key becomes valid again.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise _Refused(f"{where} is not an ISO 8601 timestamp: {value!r}")
    else:
        raise _Refused(f"{where} is not an ISO 8601 timestamp: {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _entries(keys: Any) -> tuple[Mapping[str, Any], ...]:
    """The pinned list, normalised enough to iterate safely.

    ``keys=None`` means the pinned module. Anything that is not a usable list
    of mappings is refused: a caller can pass a malformed test fixture, and
    verification must still answer rather than crash.
    """
    if keys is None:
        keys = release_keys.RELEASE_KEYS
    if isinstance(keys, (str, bytes, bytearray, Mapping)):
        raise _Refused("the pinned release key list is not a list of entries")
    try:
        values = tuple(keys)
    except TypeError:
        raise _Refused("the pinned release key list is not a list of entries")
    return tuple(entry for entry in values if isinstance(entry, Mapping))


def _select_entry(document: Mapping[str, Any], keys: Any) -> Mapping[str, Any]:
    """The pinned entry the document names, by the signed ``key_id`` member.

    Selection happens before verification because the key has to be chosen
    before anything can be checked; it is still safe because ``key_id`` is
    inside the signature's covered set. A document that changes it to point at
    another pinned key also invalidates its own signature.
    """
    key_id = document.get("key_id")
    if not isinstance(key_id, str) or not key_id:
        raise _Refused("the manifest has no key_id, so no pinned release key "
                       "can be selected")
    entries = _entries(keys)
    if not entries:
        raise _Refused(
            "no release signing key is pinned in RELEASE_KEYS, so the "
            "manifest cannot be verified")
    for entry in entries:
        if entry.get("keyId") == key_id:
            return entry
    raise _Refused(
        f"the manifest names key_id {key_id!r}, which is not pinned in "
        f"RELEASE_KEYS")


def _window(entry: Mapping[str, Any], now: datetime) -> None:
    """Refuse unless ``now`` is inside the entry's [notBefore, notAfter]."""
    key_id = entry.get("keyId")
    label = f"pinned key {key_id!r}"
    not_before = _parse_instant(entry.get("notBefore"),
                                where=f"{label} notBefore")
    not_after = _parse_instant(entry.get("notAfter"),
                               where=f"{label} notAfter")
    if not_before > not_after:
        raise _Refused(
            f"{label} has a notBefore later than its notAfter, so its window "
            f"is unusable")
    if now < not_before:
        raise _Refused(
            f"{label} is not yet valid: its notBefore is "
            f"{entry.get('notBefore')} and the manifest was checked at "
            f"{now.isoformat()}")
    if now > not_after:
        raise _Refused(
            f"{label} has expired: its notAfter was "
            f"{entry.get('notAfter')} and the manifest was checked at "
            f"{now.isoformat()}")


def _public_key(entry: Mapping[str, Any]) -> bytes:
    key_id = entry.get("keyId")
    hex_key = entry.get("publicKey")
    if not isinstance(hex_key, str):
        raise _Refused(
            f"pinned key {key_id!r} has no publicKey hex string")
    try:
        return bytes.fromhex(hex_key)
    except ValueError:
        raise _Refused(
            f"pinned key {key_id!r} has a publicKey that is not hex")


def _verify(document: Any, keys: Any, now: Any) -> tuple[bool, str]:
    if not isinstance(document, Mapping):
        raise _Refused("the manifest is not a JSON object")
    entry = _select_entry(document, keys)
    checked_at = _parse_instant(
        now if now is not None else datetime.now(timezone.utc),
        where="verification time")
    _window(entry, checked_at)
    public_key = _public_key(entry)
    ok, reason = verify_record(RELEASE_DOMAIN, document, public_key)
    if not ok:
        raise _Refused(f"key {entry.get('keyId')!r}: {reason}")
    return True, (f"valid: manifest is signed by release key "
                  f"{entry.get('keyId')!r}")


def verify_manifest(document: dict, *, keys=None,
                    now=None) -> tuple[bool, str]:
    """Check ``document`` against the pinned release keys.

    Returns ``(True, reason)`` or ``(False, reason)`` and never raises. The
    key is selected by the document's ``key_id``, the current instant must lie
    inside that entry's validity window, and only then is the signature
    checked against ``entry["publicKey"]``. Every refusal names the step that
    failed.

    ``keys`` defaults to :data:`release_keys.RELEASE_KEYS` at CALL time, so a
    test or an operator surface can pass a list without mutating the pin.
    ``now`` is a ``datetime`` (naive is read as UTC) or an ISO 8601 string;
    it defaults to the current UTC instant.
    """
    try:
        return _verify(document, keys, now)
    except _Refused as refusal:
        return False, str(refusal)
    except Exception as error:  # noqa: BLE001 - the wire is hostile
        # A verifier that raises on a malformed document is a denial of
        # service. Anything unforeseen is still a refusal, with its reason.
        return False, f"manifest verification could not complete: {error}"


def require_signed(document: dict, *, trust_unsigned: bool, keys=None,
                   now=None) -> dict | None:
    """Require a verified signature, or record the operator's opt-out.

    ``trust_unsigned=True`` returns ``None`` without consulting the pin: the
    operator surface has explicitly chosen to accept an unsigned release.
    Otherwise a verification failure raises :class:`UpdateError` with code
    ``UPDATE_UNSIGNED``, the refusal reason and the mechanical fix. On success
    the result is the pin the caller reports and re-checks: the active
    ``keyId`` and its ``notAfter``.
    """
    if trust_unsigned:
        return None
    # Snapshot an explicit pin list so a one-shot iterable is not exhausted by
    # verification before the active entry is read for the return value.
    pinned = keys
    if pinned is not None:
        try:
            pinned = _entries(pinned)
        except _Refused as refusal:
            raise UpdateError(
                "UPDATE_UNSIGNED",
                f"release manifest is not signed: {refusal}",
                fix=UNSIGNED_FIX)
    ok, reason = verify_manifest(document, keys=pinned, now=now)
    if not ok:
        raise UpdateError(
            "UPDATE_UNSIGNED",
            f"release manifest is not signed: {reason}",
            fix=UNSIGNED_FIX)
    try:
        entry = _select_entry(document, pinned)
    except _Refused as refusal:  # pragma: no cover - verify_manifest passed
        raise UpdateError("UPDATE_UNSIGNED", str(refusal), fix=UNSIGNED_FIX)
    return {"keyId": entry.get("keyId"), "notAfter": entry.get("notAfter")}

"""A peer's identity as an asymmetric key pair (issue #1278).

``peer_pool`` admits a peer on the strength of a signature. Under HMAC-SHA256
the verifier holds the same key the signer does, so the signature proves
AUTHENTICATION and nothing more: the operator that checks a join could have
produced it, and a compromised operator key forges a join for every peer whose
key it holds. That is the property a private pool of mutually distrustful
operators cannot live with, and it is what this module replaces.

The primitive is not new here
-----------------------------
``tee_quote`` already carries deterministic ECDSA over NIST P-256 and P-384
(RFC 6979), a verifier that refuses off-curve keys and out-of-range scalars, and
three test files behind it: committed vectors, an adversarial suite, and
``tests/test_ecdsa_differential.py``, which generates keys and messages and
requires OpenSSL to agree in BOTH directions including about mutated
signatures. This module wires that primitive to an identity; it does not
implement a curve, a hash or a signature encoding. Item 272 exists because three
components hand-rolled the same crypto in one wave, and a fourth would be the
same mistake with a longer key.

What a signature here proves, and under what assumption
-------------------------------------------------------
A verified signature proves that the holder of the private half of a named
public key produced these exact bytes. That is NON-REPUDIATION only under one
stated assumption, and the assumption is not cryptographic:

    the private half was generated on the peer's own machine, never left it,
    and the public half reached the verifier over a channel the verifier
    trusts (out-of-band exchange, pinned by ``key_id``).

If an operator generates a peer's key pair and hands it over, that operator can
forge the peer's signatures exactly as it could forge its MAC, and no amount of
elliptic-curve arithmetic changes it. :func:`generate_identity` draws the scalar
locally and :func:`write_private_identity` is the only function that ever writes
one, so the honest deployment is the easy one; but the assumption is a
DEPLOYMENT property, stated here rather than implied by the word "signed".

What this module deliberately does not do: it never transmits, escrows or
derives a private scalar from anything an operator supplies, and
:class:`PeerIdentity` redacts the scalar from its own ``repr`` so a logged
object is not a leaked key.

Keys have a lifecycle, and it has three ends
--------------------------------------------
An identity that cannot be rotated or revoked is worse than a shared key,
because it lasts longer. :class:`IdentityDirectory` is the pinned public half of
every peer, as a history rather than a slot:

* **generation** - :func:`generate_identity` draws a uniform scalar in ``[1, n)``
  from the host CSPRNG. :func:`identity_from_seed` is deterministic and exists
  for FIXTURES only; its docstring is the whole warning.
* **distribution** - the public half is registered against a ``peer_id`` with
  :meth:`IdentityDirectory.register`. Nothing else introduces a key. A record
  cannot introduce the key that verifies it: :func:`verify_record` is handed the
  pinned key, and the record's own ``public_key`` member is compared against it
  rather than trusted.
* **rotation** - :meth:`IdentityDirectory.rotate` registers a new key and marks
  the old one ``superseded`` from that instant. The old key keeps verifying its
  past signatures forever; it stops carrying authority for anything issued after
  the rotation.
* **revocation** - :meth:`IdentityDirectory.revoke` marks a key ``revoked``. A
  revoked key also keeps verifying: revocation cannot reach into the past and
  un-make a signature, and pretending otherwise would be the single-boolean
  answer item 546 rules out.

So :meth:`IdentityDirectory.attribute` never returns a boolean. It returns an
:class:`Attribution` carrying ``verified`` (the arithmetic: were these bytes
signed by this key?) and ``status`` (the authority: may this key act now?) as
two separate members, because they are two separate facts and the whole point of
revocation is that they come apart. ``verified=True, status="revoked"`` is the
shape of a past act that still happened and confers nothing, and it is exactly
the restore-versus-compensate distinction item 546 draws, applied to identity:
authority is restorable (revocation is its inverse), a signature having been
made is not.

Exposure: host side only
------------------------
``stdlib/crypto.rvl`` publishes ``sha256``, ``hmac_sha256``, ``ct_equal`` and
``random_token``. Nothing asymmetric is published and this module does not
publish one. The reasoning is in
``docs/design/555-asymmetric-peer-identity.md``; the short form is that signing
requires a program to hold a private scalar, which is the secret-bearing shape
the pool ladder refuses to send to a pool peer at any tier, and that publishing
a verifier alone would mean a second and a third implementation of P-256 (the
``@py`` and ``@ts`` bodies the module's tier bar requires) sitting beside the
one that OpenSSL is differentially tested against. Admitting a peer is an
operator decision taken by the ``revl pool`` host, not a computation inside a
revl program.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from .attest import NotCanonicalizable, _canonical_bytes
from .tee_quote import (
    CURVE_P256,
    Curve,
    QuoteFormatError,
    derive_public_key,
    ecdsa_sign,
    ecdsa_verify,
    private_key_from_seed,
    public_key_id,
)

#: The one curve a peer identity uses. Named rather than negotiated: a record
#: that could choose its own curve could choose a weak one, and the verifier
#: would have followed it. `tee_quote` also carries P-384 for SEV-SNP quotes;
#: that is a vendor format's choice and not this protocol's.
IDENTITY_CURVE: Curve = CURVE_P256

#: The `sign_alg` spelling an asymmetric record carries. VALIDATED, never merely
#: recorded, in the same way `attest.SIGN_ALG` and `peer_offer.SIGN_ALG` are: a
#: record cannot claim one algorithm and be checked under another.
SIGN_ALG = "ecdsa-p256-sha256"

SIGNATURE_FIELD = "signature"
PUBLIC_KEY_FIELD = "public_key"
KEY_ID_FIELD = "key_id"

#: A key registered and carrying authority now.
KEY_ACTIVE = "active"
#: A key rotated away from. It still verifies what it signed; it authorises
#: nothing issued after the rotation.
KEY_SUPERSEDED = "superseded"
#: A key withdrawn. It still verifies what it signed; it authorises nothing.
KEY_REVOKED = "revoked"
#: No such key is pinned for this peer. Not a state of a key, the absence of one.
KEY_UNKNOWN = "unknown"

#: Every status :meth:`IdentityDirectory.authority` can return. Only
#: :data:`KEY_ACTIVE` confers authority, and that is asserted by a test rather
#: than left to each reader of this list.
KEY_STATUSES: tuple[str, ...] = (KEY_ACTIVE, KEY_SUPERSEDED, KEY_REVOKED,
                                 KEY_UNKNOWN)

#: What the deployment report calls an identity backed by a key pair, and what
#: it calls one still backed by a shared secret. `peer_pool` renders these so a
#: mixed deployment is visible instead of silently weakest-link.
IDENTITY_ASYMMETRIC = "asymmetric"
IDENTITY_SHARED_KEY = "shared-key"


class IdentityError(ValueError):
    """An identity object is malformed at CONSTRUCTION or the key material is
    unusable. Distinct from a verification failure, which is returned as
    ``(False, reason)`` and never raises, because the wire is hostile and a
    verifier that raises on a bad record is a denial of service."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# the key pair
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublicIdentity:
    """The half a verifier holds: a raw ``X || Y`` P-256 point and its
    fingerprint. Non-secret by construction, which is why it is the half that
    travels."""

    peer_id: str
    public_key: bytes
    curve: Curve = IDENTITY_CURVE

    def __post_init__(self) -> None:
        if not isinstance(self.peer_id, str) or not self.peer_id:
            raise IdentityError("an identity needs a non-empty peer_id")
        if not isinstance(self.public_key, (bytes, bytearray)):
            raise IdentityError("a public key is raw X||Y bytes")
        # Refuses an off-curve point, the point at infinity and a wrong length,
        # at the boundary rather than at the first verification.
        try:
            from .tee_quote import _decode_public_key  # noqa: PLC0415

            _decode_public_key(self.curve, bytes(self.public_key))
        except QuoteFormatError as error:
            raise IdentityError(f"public key for {self.peer_id!r}: {error}")

    @property
    def key_id(self) -> str:
        """The non-secret fingerprint, in the same 16-hex spelling
        ``attest.key_id`` gives a symmetric key, so one ``key_id`` member can
        name either kind and a charter's authority view is unchanged by the
        move to key pairs."""
        return public_key_id(bytes(self.public_key))

    @property
    def public_key_hex(self) -> str:
        return bytes(self.public_key).hex()


@dataclass(frozen=True)
class PeerIdentity:
    """A peer's own key pair. The private scalar lives here and nowhere else.

    ``repr`` is overridden to redact the scalar. That is not decoration: a
    dataclass repr of a signing key ends up in a traceback, a log line or a
    pytest failure message, and a key that leaks that way is as compromised as
    one that leaked any other way."""

    peer_id: str
    private_key: int
    curve: Curve = IDENTITY_CURVE

    def __post_init__(self) -> None:
        if not isinstance(self.peer_id, str) or not self.peer_id:
            raise IdentityError("an identity needs a non-empty peer_id")
        if not isinstance(self.private_key, int) or \
                isinstance(self.private_key, bool):
            raise IdentityError("a private key is an integer scalar")
        if not 1 <= self.private_key < self.curve.n:
            raise IdentityError(
                f"a {self.curve.name} private scalar must lie in [1, n); the "
                f"one given does not, so it is not a key")

    def __repr__(self) -> str:  # pragma: no cover - exercised by a test
        return (f"PeerIdentity(peer_id={self.peer_id!r}, "
                f"private_key=<redacted>, curve={self.curve.name!r})")

    @property
    def public_key(self) -> bytes:
        return derive_public_key(self.curve, self.private_key)

    @property
    def key_id(self) -> str:
        return public_key_id(self.public_key)

    def public(self) -> PublicIdentity:
        return PublicIdentity(peer_id=self.peer_id, public_key=self.public_key,
                              curve=self.curve)


def generate_identity(peer_id: str, *,
                      curve: Curve = IDENTITY_CURVE) -> PeerIdentity:
    """A fresh key pair, drawn from the host CSPRNG.

    ``secrets.randbelow(n - 1) + 1`` is uniform on ``[1, n - 1]``, which is the
    whole valid scalar range, with no modular bias to argue about. The draw
    happens here, on the machine that will hold the key; nothing in this module
    moves a private scalar anywhere."""
    if not isinstance(peer_id, str) or not peer_id:
        raise IdentityError("an identity needs a non-empty peer_id")
    return PeerIdentity(peer_id=peer_id,
                        private_key=secrets.randbelow(curve.n - 1) + 1,
                        curve=curve)


def identity_from_seed(peer_id: str, seed: bytes, *,
                       curve: Curve = IDENTITY_CURVE) -> PeerIdentity:
    """A DETERMINISTIC identity, for fixtures and tests.

    The scalar is a hash of the seed, so its secrecy is exactly the seed's. A
    seed committed to a repository is a published private key. Use
    :func:`generate_identity` for anything that admits a real peer."""
    if not isinstance(seed, (bytes, bytearray)) or not seed:
        raise IdentityError("an identity seed must be non-empty bytes")
    return PeerIdentity(peer_id=peer_id,
                        private_key=private_key_from_seed(curve, bytes(seed)),
                        curve=curve)


# ---------------------------------------------------------------------------
# signing a record
# ---------------------------------------------------------------------------


def signed_bytes(domain: bytes, body: Mapping[str, Any]) -> bytes:
    """``domain`` ++ the canonical bytes of every member of ``body`` EXCEPT the
    signature.

    The covered set is DERIVED FROM THE RECORD, never from a hand-written field
    list. That is item 517's discipline and the defence against the failure it
    was written after: a signed receipt in this tree once covered a fixed list
    while its body grew a ``key_id`` afterwards, so the member every reader used
    to pick a verification key was the one member the signature did not cover.
    There is no list here to fall out of date.

    Canonical JSON sorts members, so reordering a record's members does not
    change these bytes and cannot change a verdict. REMOVING one does change
    them, which is why truncation is a signature failure and not a silent
    default."""
    covered = {k: v for k, v in body.items() if k != SIGNATURE_FIELD}
    return bytes(domain) + _canonical_bytes(covered)


def sign_record(domain: bytes, body: Mapping[str, Any],
                identity: PeerIdentity) -> dict:
    """Sign ``body`` under ``identity``, returning the record plus its
    signature.

    The returned record carries ``sign_alg``, ``key_id`` and ``public_key``, and
    all three are INSIDE the covered set: a record whose embedded public key was
    swapped for an attacker's does not verify, so the substitution is a
    signature failure rather than a successful verification under the wrong
    key.

    Deterministic: RFC 6979 nonces mean the same ``(body, identity)`` always
    produces the same bytes, which is what lets a fixture be regenerated and a
    digest be pinned."""
    if not isinstance(identity, PeerIdentity):
        raise IdentityError("signing needs a PeerIdentity, not a raw key")
    record = dict(body)
    record["sign_alg"] = SIGN_ALG
    record[KEY_ID_FIELD] = identity.key_id
    record[PUBLIC_KEY_FIELD] = identity.public_key.hex()
    record[SIGNATURE_FIELD] = ecdsa_sign(
        identity.curve, identity.private_key,
        signed_bytes(domain, record)).hex()
    return record


def verify_record(domain: bytes, record: Mapping[str, Any],
                  public_key: bytes, *,
                  curve: Curve = IDENTITY_CURVE) -> tuple[bool, str]:
    """Check ``record`` against a public key the VERIFIER already pins.

    Returns ``(ok, reason)`` and never raises, for the same reason
    ``peer_offer.verify_offer`` does not: a hostile record must not be able to
    turn a refusal into a crash.

    ``public_key`` is the key the verifier trusts, not the one the record
    carries. The record's own ``public_key`` member is COMPARED against it and a
    mismatch is refused; it is never read as the key to verify under. A verifier
    that took the key from the record would accept every record an attacker
    signed with an attacker's key, which is the failure this ordering exists to
    make impossible."""
    if not isinstance(public_key, (bytes, bytearray)) or not public_key:
        return False, "no verification key provided"
    if not isinstance(record, Mapping):
        return False, "record is not an object"
    if record.get("sign_alg") != SIGN_ALG:
        return False, (f"sign_alg is {record.get('sign_alg')!r}, expected "
                       f"{SIGN_ALG!r}")
    given = record.get(SIGNATURE_FIELD)
    if not isinstance(given, str):
        return False, "record has no signature"
    try:
        signature = bytes.fromhex(given)
    except ValueError:
        return False, "signature is not hex"
    embedded = record.get(PUBLIC_KEY_FIELD)
    if not isinstance(embedded, str):
        return False, f"public_key is not a hex string ({embedded!r})"
    try:
        embedded_bytes = bytes.fromhex(embedded)
    except ValueError:
        return False, "public_key is not hex"
    if embedded_bytes != bytes(public_key):
        return False, ("the record names a different public key than the one "
                       "pinned for this signer; a record does not get to "
                       "choose the key it is checked against")
    if record.get(KEY_ID_FIELD) != public_key_id(bytes(public_key)):
        return False, (f"key_id is {record.get(KEY_ID_FIELD)!r}, which is not "
                       f"the fingerprint of the pinned public key")
    try:
        message = signed_bytes(domain, record)
    except NotCanonicalizable as error:
        return False, f"record cannot be verified: {error}"
    try:
        ok = ecdsa_verify(curve, bytes(public_key), message, signature)
    except QuoteFormatError as error:
        # The VERIFIER's own pinned key is not a point on the curve. That is a
        # configuration fault where it can be fixed, not a peer's record being
        # rejected, so it is reported as one rather than as a bad signature.
        return False, f"the pinned public key is unusable: {error}"
    if not ok:
        return False, ("signature mismatch: wrong key, or the record was "
                       "edited after signing")
    return True, "valid: record is authentic under the pinned public key"


# ---------------------------------------------------------------------------
# the directory: pinned public halves, with a history
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyRecord:
    """One pinned public key of one peer, with the window it carried authority
    in.

    ``not_after`` is set when the key is rotated away from or revoked. It does
    not stop the key verifying anything; it stops the key AUTHORISING anything
    issued at or after that instant."""

    peer_id: str
    key_id: str
    public_key_hex: str
    status: str
    not_before: str
    not_after: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in KEY_STATUSES or self.status == KEY_UNKNOWN:
            raise IdentityError(
                f"a pinned key's status is one of "
                f"{', '.join(s for s in KEY_STATUSES if s != KEY_UNKNOWN)}; "
                f"got {self.status!r}")

    @property
    def public_key(self) -> bytes:
        return bytes.fromhex(self.public_key_hex)

    def as_dict(self) -> dict:
        return {"peer_id": self.peer_id, "key_id": self.key_id,
                "public_key": self.public_key_hex, "status": self.status,
                "not_before": self.not_before, "not_after": self.not_after,
                "reason": self.reason}


@dataclass(frozen=True)
class Attribution:
    """Who signed this, and may that signer act.

    Two members because they are two facts, and revocation is precisely the
    event that pulls them apart. ``verified`` is arithmetic over bytes and never
    changes once true. ``status`` is a live authority reading and does. A caller
    that wants one answer asks :attr:`confers_authority`, which is the
    conjunction spelled out, so nobody has to remember which of the two the bare
    boolean meant."""

    peer_id: str
    key_id: str
    verified: bool
    status: str
    reason: str

    @property
    def confers_authority(self) -> bool:
        """Only an ACTIVE key that actually verified authorises anything. A
        superseded or revoked key that verified is a true statement about the
        past and a refusal in the present."""
        return self.verified and self.status == KEY_ACTIVE

    def as_dict(self) -> dict:
        return {"peer_id": self.peer_id, "key_id": self.key_id,
                "verified": self.verified, "status": self.status,
                "confers_authority": self.confers_authority,
                "reason": self.reason}


class IdentityDirectory:
    """Every public key the operator pins, per peer, as a history.

    A slot would lose the thing that matters: after a rotation the old key must
    still be findable, because the ledger is full of records it signed and a
    reader that cannot find the key cannot check the history. So keys are
    appended and marked, never replaced and never deleted."""

    KIND = "revl.peer-identities"
    VERSION = "1.0"

    def __init__(self) -> None:
        self.keys: dict[str, list[KeyRecord]] = {}

    # -- lifecycle --------------------------------------------------------

    def register(self, identity: PublicIdentity, *,
                 at: Optional[datetime] = None) -> KeyRecord:
        """Pin a peer's public key. The only way a key enters this directory.

        Refuses a peer that already has an active key: replacing one is
        :meth:`rotate`, which records what it replaced. A silent overwrite would
        make a rotation indistinguishable from a first registration in the
        ledger, and the difference is the whole audit trail."""
        if not isinstance(identity, PublicIdentity):
            raise IdentityError("register takes a PublicIdentity")
        when = _iso(at or _utc_now())
        existing = self.keys.setdefault(identity.peer_id, [])
        for record in existing:
            if record.status == KEY_ACTIVE:
                raise IdentityError(
                    f"peer {identity.peer_id!r} already has an active key "
                    f"{record.key_id}; use rotate() so the replacement is "
                    f"recorded as one")
            if record.key_id == identity.key_id:
                raise IdentityError(
                    f"key {identity.key_id} was already pinned for "
                    f"{identity.peer_id!r} and is {record.status}; a withdrawn "
                    f"key is not re-registered")
        pinned = KeyRecord(peer_id=identity.peer_id, key_id=identity.key_id,
                           public_key_hex=identity.public_key_hex,
                           status=KEY_ACTIVE, not_before=when)
        existing.append(pinned)
        return pinned

    def rotate(self, identity: PublicIdentity, *, reason: str = "rotation",
               at: Optional[datetime] = None) -> tuple[Optional[KeyRecord], KeyRecord]:
        """Replace a peer's active key, keeping the old one findable.

        Returns ``(superseded, active)``. The old key's ``not_after`` is this
        instant: everything it signed before then still verifies AND still
        attributes to that peer, and nothing it signs afterwards carries
        authority. Rotation is not revocation and this module does not conflate
        them: a rotated key is assumed uncompromised, so its past acts remain
        attributable without a caveat."""
        when = at or _utc_now()
        stamp = _iso(when)
        history = self.keys.setdefault(identity.peer_id, [])
        superseded: Optional[KeyRecord] = None
        for index, record in enumerate(history):
            if record.status == KEY_ACTIVE:
                superseded = KeyRecord(
                    peer_id=record.peer_id, key_id=record.key_id,
                    public_key_hex=record.public_key_hex,
                    status=KEY_SUPERSEDED, not_before=record.not_before,
                    not_after=stamp, reason=reason)
                history[index] = superseded
                break
        if any(record.key_id == identity.key_id for record in history):
            raise IdentityError(
                f"key {identity.key_id} is already in {identity.peer_id!r}'s "
                f"history; rotating to a key the peer already used is not a "
                f"rotation")
        active = KeyRecord(peer_id=identity.peer_id, key_id=identity.key_id,
                           public_key_hex=identity.public_key_hex,
                           status=KEY_ACTIVE, not_before=stamp)
        history.append(active)
        return superseded, active

    def revoke(self, peer_id: str, key_id: str, *, reason: str,
               at: Optional[datetime] = None) -> KeyRecord:
        """Withdraw a key's authority. It keeps verifying; it stops acting.

        This is the half a shared-key pool could not offer. Revoking a shared
        key destroys the ability to check what the peer did, because the same
        key was the verifier. Revoking a public key destroys nothing: the ledger
        stays checkable by anyone holding the public half, which is the point of
        having published it."""
        record = self.lookup(peer_id, key_id)
        if record is None:
            raise IdentityError(
                f"no key {key_id} is pinned for peer {peer_id!r}; there is "
                f"nothing to revoke")
        if record.status == KEY_REVOKED:
            return record
        stamp = _iso(at or _utc_now())
        revoked = KeyRecord(
            peer_id=record.peer_id, key_id=record.key_id,
            public_key_hex=record.public_key_hex, status=KEY_REVOKED,
            not_before=record.not_before,
            not_after=record.not_after or stamp, reason=reason)
        history = self.keys[peer_id]
        history[history.index(record)] = revoked
        return revoked

    # -- reading ----------------------------------------------------------

    def lookup(self, peer_id: str, key_id: str) -> Optional[KeyRecord]:
        """One pinned key of one peer, by fingerprint.

        Selecting by a fingerprint a RECORD supplied is safe here and only here:
        the search space is the keys this directory already pinned for that
        peer, so a hostile record can pick among a peer's own keys and cannot
        introduce one. Picking the peer itself from a record is a different
        question and ``peer_pool`` answers it before calling this."""
        for record in self.keys.get(peer_id, ()):
            if record.key_id == key_id:
                return record
        return None

    def active(self, peer_id: str) -> Optional[KeyRecord]:
        for record in self.keys.get(peer_id, ()):
            if record.status == KEY_ACTIVE:
                return record
        return None

    def has_identity(self, peer_id: str) -> bool:
        """Does this peer have an asymmetric identity AT ALL, in any state?

        The question a downgrade check asks. A peer whose key was revoked has
        not stopped being an asymmetric peer, so this is deliberately not
        ``active(peer_id) is not None``: otherwise revoking a key would move the
        peer back onto the shared-key path, which is the downgrade the
        revocation was meant to prevent."""
        return bool(self.keys.get(peer_id))

    def authority(self, peer_id: str, key_id: str, *,
                  when: Optional[datetime] = None) -> tuple[str, str]:
        """``(status, reason)`` for a key at an instant. Fail-closed: a key this
        directory has never seen is :data:`KEY_UNKNOWN`, never a default."""
        record = self.lookup(peer_id, key_id)
        if record is None:
            return KEY_UNKNOWN, (
                f"no key {key_id} is pinned for peer {peer_id!r}; an identity "
                f"nobody registered is not an identity")
        if record.status == KEY_REVOKED:
            return KEY_REVOKED, (
                f"key {key_id} was revoked at {record.not_after}"
                f"{f' ({record.reason})' if record.reason else ''}; its past "
                f"signatures still verify and it authorises nothing")
        if record.status == KEY_SUPERSEDED:
            return KEY_SUPERSEDED, (
                f"key {key_id} was rotated away from at {record.not_after}; it "
                f"still verifies what it signed before then and authorises "
                f"nothing now")
        moment = when or _utc_now()
        starts = _parse_iso(record.not_before)
        if starts is not None and moment < starts:
            return KEY_UNKNOWN, (
                f"key {key_id} was pinned at {record.not_before}, after the "
                f"instant being checked")
        return KEY_ACTIVE, f"key {key_id} is the active key for {peer_id!r}"

    def attribute(self, domain: bytes, record: Mapping[str, Any], peer_id: str,
                  *, when: Optional[datetime] = None) -> Attribution:
        """Who signed ``record``, and may that signer act now.

        The two questions are answered separately and BOTH are answered even
        when one of them fails, because "this signature is real but the key is
        revoked" and "this signature is a forgery" are different findings and a
        reader that cannot tell them apart cannot audit anything."""
        key_id_claimed = record.get(KEY_ID_FIELD) if isinstance(record, Mapping) \
            else None
        if not isinstance(key_id_claimed, str):
            return Attribution(peer_id=peer_id, key_id="", verified=False,
                               status=KEY_UNKNOWN,
                               reason="the record names no key_id")
        pinned = self.lookup(peer_id, key_id_claimed)
        if pinned is None:
            return Attribution(
                peer_id=peer_id, key_id=key_id_claimed, verified=False,
                status=KEY_UNKNOWN,
                reason=(f"no key {key_id_claimed} is pinned for peer "
                        f"{peer_id!r}"))
        ok, reason = verify_record(domain, record, pinned.public_key)
        status, status_reason = self.authority(peer_id, key_id_claimed,
                                               when=when)
        return Attribution(peer_id=peer_id, key_id=key_id_claimed, verified=ok,
                           status=status,
                           reason=reason if not ok else status_reason)

    # -- serialization ----------------------------------------------------

    def as_dict(self) -> dict:
        return {"kind": self.KIND, "version": self.VERSION,
                "peers": {peer: [record.as_dict() for record in history]
                          for peer, history in sorted(self.keys.items())}}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "IdentityDirectory":
        directory = cls()
        for peer, history in payload.get("peers", {}).items():
            directory.keys[peer] = [
                KeyRecord(peer_id=spec.get("peer_id", peer),
                          key_id=spec["key_id"],
                          public_key_hex=spec["public_key"],
                          status=spec.get("status", KEY_ACTIVE),
                          not_before=spec.get("not_before", ""),
                          not_after=spec.get("not_after", ""),
                          reason=spec.get("reason", ""))
                for spec in history]
        return directory

    def summary(self) -> list[dict]:
        """One row per peer for an operator report: which key is live and how
        many are behind it."""
        rows = []
        for peer, history in sorted(self.keys.items()):
            live = self.active(peer)
            rows.append({
                "peer_id": peer,
                "active_key_id": live.key_id if live else "",
                "status": live.status if live else KEY_REVOKED,
                "superseded": sum(1 for r in history
                                  if r.status == KEY_SUPERSEDED),
                "revoked": sum(1 for r in history if r.status == KEY_REVOKED),
            })
        return rows


# ---------------------------------------------------------------------------
# key files
# ---------------------------------------------------------------------------

PRIVATE_KIND = "revl.peer-identity-private"
PUBLIC_KIND = "revl.peer-identity-public"


def write_private_identity(path, identity: PeerIdentity) -> None:
    """Write a peer's own key pair, private half included, at mode 0600.

    The only function in this module that puts a private scalar on disk, and it
    is on the peer's own machine by construction: the operator never calls it
    for someone else's peer, because the whole property this module buys is lost
    the moment two parties hold the same private half."""
    import os  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"kind": PRIVATE_KIND, "version": "1.0",
               "peer_id": identity.peer_id, "curve": identity.curve.name,
               "key_id": identity.key_id,
               "public_key": identity.public_key.hex(),
               "private_key": format(identity.private_key, "064x")}
    handle = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
    os.chmod(str(target), 0o600)


def load_private_identity(path) -> PeerIdentity:
    from pathlib import Path  # noqa: PLC0415

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("kind") != PRIVATE_KIND:
        raise IdentityError(
            f"{path} is {payload.get('kind')!r}, not a {PRIVATE_KIND!r} file")
    curve = IDENTITY_CURVE
    if payload.get("curve") != curve.name:
        raise IdentityError(
            f"{path} names curve {payload.get('curve')!r}; a peer identity is "
            f"{curve.name}")
    return PeerIdentity(peer_id=payload["peer_id"],
                        private_key=int(payload["private_key"], 16),
                        curve=curve)


def write_public_identity(path, identity: PublicIdentity) -> None:
    """Write the half that travels. This is the file an operator is handed out
    of band and pins."""
    from pathlib import Path  # noqa: PLC0415

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(
        {"kind": PUBLIC_KIND, "version": "1.0", "peer_id": identity.peer_id,
         "curve": identity.curve.name, "key_id": identity.key_id,
         "public_key": identity.public_key_hex},
        indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_public_identity(path) -> PublicIdentity:
    from pathlib import Path  # noqa: PLC0415

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("kind") not in (PUBLIC_KIND, PRIVATE_KIND):
        raise IdentityError(
            f"{path} is {payload.get('kind')!r}, not a peer identity file")
    return PublicIdentity(peer_id=payload["peer_id"],
                          public_key=bytes.fromhex(payload["public_key"]))


def fingerprint_of(public_key_hex: str) -> str:
    """The ``key_id`` of a hex-encoded public key, for an operator checking that
    the file they were handed is the key they were told to expect."""
    return public_key_id(bytes.fromhex(public_key_hex))

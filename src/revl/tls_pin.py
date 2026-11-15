"""A remote row's pinned peer identity: the TLS leaf SubjectPublicKeyInfo.

Issue #2719. A `remote` row names an ADDRESS (`at host("h:port")`) and, until
this module, nothing about the party that answers there: a different key
presenting a valid certificate at the same address was indistinguishable from
the intended peer. The pin binds the row to the key the WIRE already presents,
so the first version adds no signature to the canonical envelope and no key
server: the TLS handshake is the presentation, and the pin is a check on it.

What the pin is, exactly
------------------------
The value on the row is the FULL SHA-256 hex of the peer certificate's
SubjectPublicKeyInfo (SPKI), the DER structure `openssl x509 -pubkey` emits
before PEM framing. It is a hash of a key, not of a certificate, so a peer may
renew or re-issue its certificate without breaking the pin as long as the key
stays the same; rotating the key is exactly the event the pin notices.

The ordinary TLS chain and hostname verification still runs first, and this
module does not weaken it: the emitted handler keeps the same context urllib
would build itself, and only AFTER that succeeds does it compare the presented
SPKI to the pin. So the pin is an ADDITIONAL constraint, never a substitute
for the CA and the name.

What this deliberately does not do
----------------------------------
* No signature is added to the request or the reply envelope. The canonical
  wire stays `{"key","method","args"}` / `{"ok","value"|"error"}`.
* No revocation source is introduced. A revoked or rotated key is a pin
  mismatch, and a pin mismatch follows the row's failure mode: a
  `TransportFault` under the default `on_failure(withdraw)` (so the runtime
  withdraws the provider), or the in-band `Err` under `on_failure(result)`.
  Directory-backed revocation is deferred to a design note (issue #2719).

The DER reader below is intentionally small: it walks the certificate's
SEQUENCE fields to the SubjectPublicKeyInfo and hashes that raw TLV. It does
not parse names, extensions or signatures, because nothing here reads them —
the chain validator already did. Malformed lengths and unexpected tags raise,
and the emitted handler turns that into the same transport fault a mismatch
produces rather than crashing with an index error.
"""

from __future__ import annotations

import hashlib
import re

from .errors import RevlError

#: A pin is the full SHA-256 digest, so 32 bytes, 64 hex characters.
PIN_HEX_LEN = 64

_PIN_RE = re.compile(rf"^[0-9a-fA-F]{{{PIN_HEX_LEN}}}$")

#: The tag bytes the certificate walk expects, in order, after the optional
#: `[0] EXPLICIT version`: serialNumber (INTEGER), signature (SEQUENCE), issuer
#: (SEQUENCE), validity (SEQUENCE), subject (SEQUENCE). The next field is the
#: SubjectPublicKeyInfo SEQUENCE this module hashes.
_TBS_FIELDS = (0x02, 0x30, 0x30, 0x30, 0x30)


def normalize_pin(pin: str) -> str:
    """Lower-case the hex spelling after refusing anything but a full digest.

    Upper-case is accepted and normalised rather than refused: the digest is
    the same value, and a hexadecimal hash has never carried meaning in its
    letter case. A short digest is refused, not padded — an operator who wrote
    the first sixteen characters of `sha256sum` output must be told the pin is
    incomplete rather than silently getting a weaker one.
    """
    if not isinstance(pin, str) or not _PIN_RE.match(pin):
        raise ValueError(
            "a pin is the full SHA-256 hex of the peer's TLS leaf "
            "SubjectPublicKeyInfo: 64 hex characters (32 bytes)")
    return pin.lower()


def check_pin(pin: str | None, *, doc: str, line: int, label: str) -> str | None:
    """Admission: refuse a malformed pin, return the normalised one."""
    if pin is None:
        return None
    try:
        return normalize_pin(pin)
    except ValueError as exc:
        raise RevlError(
            doc, line,
            f"the pinned identity of remote row `@{label}` is malformed: "
            f"{pin!r}",
            hint="the pin is the FULL SHA-256 hex (64 hex characters) of the "
                 "peer's TLS leaf SubjectPublicKeyInfo, the DER key "
                 "`openssl x509 -pubkey -noout` names. Compute it with "
                 "`openssl x509 -in peer.crt -pubkey -noout | "
                 "openssl pkey -pubin -outform DER | sha256sum`. A short "
                 "digest is refused rather than padded (issue #2719)") from exc


def _tlv(buf: bytes, off: int) -> tuple[int, int, int]:
    """Read one DER TLV: `(tag, content start, end)`.

    Only the definite-length forms a certificate uses are accepted. An
    indefinite length is not DER and is refused; an over-long or truncated
    length is refused rather than clipped.
    """
    if off + 2 > len(buf):
        raise ValueError("truncated DER")
    tag = buf[off]
    first = buf[off + 1]
    start = off + 2
    if first & 0x80:
        count = first & 0x7F
        if count == 0 or count > 4 or start + count > len(buf):
            raise ValueError("non-DER length")
        length = int.from_bytes(buf[start:start + count], "big")
        if length < 0x80:
            raise ValueError("non-minimal length")
        start += count
    else:
        length = first
    end = start + length
    if end > len(buf):
        raise ValueError("truncated DER")
    return tag, start, end


def subject_public_key_info(cert_der: bytes) -> bytes:
    """The SubjectPublicKeyInfo TLV of one DER certificate, byte for byte.

    The walk is positional because a certificate is a fixed sequence at this
    depth: Certificate -> tbsCertificate -> [version] serial, signature,
    issuer, validity, subject, subjectPublicKeyInfo. Nothing after the SPKI is
    read, so issuer/subject unique ids and every extension stay untouched.
    """
    tag, body, _end = _tlv(cert_der, 0)
    if tag != 0x30:
        raise ValueError("a certificate is a SEQUENCE")
    tag, tbs, tbs_end = _tlv(cert_der, body)
    if tag != 0x30:
        raise ValueError("tbsCertificate is a SEQUENCE")
    off = tbs
    if off < tbs_end and cert_der[off] == 0xA0:   # [0] EXPLICIT version
        _tag, _content, off = _tlv(cert_der, off)
    for want in _TBS_FIELDS:
        tag, _content, off = _tlv(cert_der, off)
        if tag != want:
            raise ValueError("unexpected certificate field before "
                             "subjectPublicKeyInfo")
    start = off
    tag, _content, end = _tlv(cert_der, start)
    if tag != 0x30 or end > tbs_end:
        raise ValueError("subjectPublicKeyInfo is a SEQUENCE")
    return cert_der[start:end]


def spki_sha256(cert_der: bytes) -> str:
    """The full SHA-256 hex of a DER certificate's SubjectPublicKeyInfo."""
    return hashlib.sha256(subject_public_key_info(cert_der)).hexdigest()


def py_pin_handler(pin: str) -> str:
    """Python source for the pinned HTTPS handler, spliced into an emitted body.

    Indented four spaces, like `crossing_redirect.py_policy`, because both are
    spliced into a host body that sits at one level of function indentation.
    The pin must already be normalised; the caller admits it once.

    The handler subclasses the standard `HTTPSHandler` and the standard
    `HTTPSConnection` so the emitted body keeps exactly the context urllib
    would have built (system trust store, `CERT_REQUIRED`, hostname checking):
    `super().connect()` performs those checks, and only then is the presented
    leaf read and hashed. A chain or hostname failure therefore never reaches
    the pin comparison, and the pin can only make the check stricter.
    """
    return f'''
    # -- pinned peer identity (revl.tls_pin, issue #2719) ----------------
    # The pin names the key the WIRE presents: the TLS leaf
    # SubjectPublicKeyInfo, as a full SHA-256 hex. The ordinary chain and
    # hostname check still runs first; this only refuses a peer that presents
    # a different key at the same address. No signature is added to the
    # canonical envelope, and directory-backed revocation is deferred to a
    # design note.
    _revl_pin_expected = {pin!r}

    def _revl_tlv(_buf, _off):
        if _off + 2 > len(_buf):
            raise ValueError("truncated DER")
        _tag = _buf[_off]
        _first = _buf[_off + 1]
        _start = _off + 2
        if _first & 0x80:
            _count = _first & 0x7F
            if _count == 0 or _count > 4 or _start + _count > len(_buf):
                raise ValueError("non-DER length")
            _len = int.from_bytes(_buf[_start:_start + _count], "big")
            _start += _count
        else:
            _len = _first
        _end = _start + _len
        if _end > len(_buf):
            raise ValueError("truncated DER")
        return _tag, _start, _end

    def _revl_spki(_cert):
        _tag, _body, _end = _revl_tlv(_cert, 0)
        if _tag != 0x30:
            raise ValueError("certificate is not a SEQUENCE")
        _tag, _tbs, _tbs_end = _revl_tlv(_cert, _body)
        if _tag != 0x30:
            raise ValueError("tbsCertificate is not a SEQUENCE")
        _p = _tbs
        if _p < _tbs_end and _cert[_p] == 0xA0:
            _tag, _s, _p = _revl_tlv(_cert, _p)
        for _want in (0x02, 0x30, 0x30, 0x30, 0x30):
            _tag, _s, _p = _revl_tlv(_cert, _p)
            if _tag != _want:
                raise ValueError("unexpected certificate field")
        _start = _p
        _tag, _s, _end = _revl_tlv(_cert, _start)
        if _tag != 0x30 or _end > _tbs_end:
            raise ValueError("subjectPublicKeyInfo is not a SEQUENCE")
        return _cert[_start:_end]

    def _revl_spki_sha256(_cert):
        return _hashlib.sha256(_revl_spki(_cert)).hexdigest()

    class _PinnedHTTPSConnection(_http_client.HTTPSConnection):
        def connect(self):
            super().connect()
            try:
                _presented = _revl_spki_sha256(
                    self.sock.getpeercert(binary_form=True))
            except Exception as _exc:
                self.close()
                raise TransportFault(
                    "remote: could not read the peer's TLS leaf certificate "
                    "for the pinned identity") from _exc
            if _presented != _revl_pin_expected:
                self.close()
                raise TransportFault(
                    "remote: TLS leaf public key does not match the pinned "
                    "identity: expected sha256:%s, presented sha256:%s"
                    % (_revl_pin_expected, _presented))

    class _PinnedHTTPSHandler(_req.HTTPSHandler):
        def https_open(self, _request):
            return self.do_open(_PinnedHTTPSConnection, _request,
                                context=self._context)
'''


#: The imports the pin handler needs beyond the emitted body's own. Kept beside
#: the source so a caller cannot add the handler without them.
PY_PIN_IMPORTS = "import hashlib as _hashlib, http.client as _http_client"

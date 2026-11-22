"""Signed, chained receipts for consequential actions (issue #2757).

Design note: `docs/design/570-action-receipt-chain.md`.

revl already signs single records: `attest` signs a gate verdict, `deploy`
signs an admission receipt, `erasure_receipt` signs an erasure report,
`model_evidence` seals a model decision, and `pool_receipt` signs an execution
receipt. Each of those is ONE record kind about ONE artifact. An operator
auditing a run could show any one of them and still not answer the question the
run exists to answer: what did this session DO, in order, and can a reader tell
that nothing was removed?

This module is the per-action receipt and the chain over receipts. It binds the
six things the item names, for every consequential action:

  * the bundle and the policy in force, by digest (or an explicit ``null`` when
    the run has neither, never an omitted member);
  * the approval used, when there was one;
  * the capability crossed;
  * a commitment to the input and a commitment to the result;
  * the worker identity and the session verdict;
  * the digest of the previous receipt, which is what makes the record a CHAIN
    rather than a pile of signed documents.

Two receipt kinds are defined in this slice, from the item's own proposal:
``effect-crossing`` (a boundary crossing that happened) and ``approval-use``
(the spend of an approval). Both carry the FULL body; a kind selects which
binding members are required, never which are allowed, because a receipt that
thins its body by kind recreates the "which field was dropped" question at
audit time (the rule `revl.ui_action` states for one UI step).

Why a chain and not a WAL hash chain
------------------------------------
Item 413.3 deferred a previous-line hash chain on the WAL because an UNKEYED
chain over byte-identical writers is recomputable by anyone who can rewrite the
file. A receipt here is signed, so altering one field, removing a receipt or
reordering two leaves a broken link and a broken MAC that no file-level rewrite
can repair without the key. The chain is not a replacement for the WAL: the
WAL is the runtime's recovery surface and this is the audit artifact exported
from a session.

The commitments
---------------
``input`` and ``result`` are commitment objects, never raw values::

    {mode, value, reason}

``mode`` is one of :data:`COMMITMENT_MODES`.

  * ``salted``: ``value`` is a SHA-256 over a domain tag, the chain's salt,
    the commitment's label (``input`` or ``result``) and the canonical spelling
    of the value. The salt makes two chains non-comparable and defeats
    precomputed tables. It does NOT hide a low-entropy value from a reader who
    holds the salt, so it is not a confidentiality mechanism.
  * ``suppressed``: there is no value and ``reason`` names which origin
    caused the suppression. A commitment over a ``secret`` or ``confidential``
    value would be a confirmation oracle: an adversary who can guess a
    candidate value can confirm it against the digest. The tree already
    refuses that for model prompts (`revl.model_evidence` §4 and
    `backends/python/runtime.py:revl_prompt_digest`); this module applies the
    same rule to a consequential action and, unlike a budgeted digest, refuses
    to mint the record at all rather than sealing it with a weakened value.

A verifier enforces the disclosure rule in BOTH directions: origins that
include a disclosure class REQUIRE a suppressed commitment with the matching
reason, and origins that do not carry one require a salted commitment. A signer
holding the key cannot mint a receipt that leaks a secret digest and cannot
suppress an ordinary value without a named disclosure origin.

Key custody, rotation and anchoring are decisions recorded in the design note.
The implementation's share of them is small and strict: a key is always
resolved from outside the tree (an explicit path or an environment variable,
never a default), each receipt names a non-secret ``key_id`` fingerprint so a
verifier chooses the right key, and a chain's envelope signature covers the
count and the head so truncating the tail is visible without an external log.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from . import attest
from .attest import NotCanonicalizable, _canonical_bytes
from .errors import RevlError
from .taint import _ORIGIN_CLASSES

# ---------------------------------------------------------------------------
# envelope identity
# ---------------------------------------------------------------------------

#: The self-identifying tag of one receipt, the same kind/version idea
#: `revl.attest`, `revl.model_evidence` and `revl.anchor` use.
RECEIPT_KIND = "revl.action-receipt"
RECEIPT_VERSION = "1.0"

#: The self-identifying tag of the chain document that carries receipts.
CHAIN_KIND = "revl.action-receipt-chain"
CHAIN_VERSION = "1.0"

#: One value today, VALIDATED rather than recorded: the MAC below is
#: unconditionally HMAC-SHA256, so a record claiming another algorithm is a
#: mislabel and is refused (the algorithm-confusion downgrade item 428 F1
#: measured on `revl attest`).
SIGN_ALG = "hmac-sha256"
HASH_ALG = "sha256"

#: Domain separation for the two MACs this module writes. One key may sign
#: attestations, deploy receipts, anchors and receipts; without a distinct tag
#: one protocol's record verifies as another's.
SIGN_DOMAIN = b"revl.action-receipt/v1\x00"
CHAIN_SIGN_DOMAIN = b"revl.action-receipt-chain/v1\x00"

#: Domain separation inside a commitment digest, so a commitment value cannot
#: be read as a bare SHA-256 of the value.
COMMITMENT_DOMAIN = b"revl.action-receipt-commitment/v1\x00"

#: The two receipt kinds this slice defines. A kind names which binding
#: members are REQUIRED; it never narrows the body.
RECEIPT_KINDS = ("effect-crossing", "approval-use")

#: The session verdict a chain binds, from the session commit protocol
#: (`docs/design/245-session-commit.md`): the presence of `commit-approved` is
#: the commit verdict and its absence is the abort verdict, including a crash.
#: There is deliberately no third "unknown": a chain is exported at a session
#: boundary, not streamed while the verdict is still open.
SESSION_VERDICTS = ("committed", "aborted")

#: Commitment modes. See the module header.
COMMITMENT_MODES = ("salted", "suppressed")

#: Item 249's origin lattice, read from :mod:`revl.taint` rather than copied.
ORIGIN_CLASSES = tuple(sorted(_ORIGIN_CLASSES))

#: The origins whose values a commitment must never digest. The runtime's own
#: prompt gate refuses the same two (`DISCLOSURE_ORIGINS` in
#: `revl.model_evidence`), and this is a second path to the same value class.
DISCLOSURE_ORIGINS = ("confidential", "secret")

#: Why a suppressed commitment has no value. The secret arm wins when a value
#: carries both, so the reason is a pure function of the origins rather than of
#: their order.
SUPPRESSION_REASONS = ("confidential-origin", "secret-origin")

#: The two labels a commitment can carry. The label is inside the digest so an
#: input commitment cannot be moved into the result member even by a key
#: holder who re-signs the surrounding record.
COMMITMENT_LABELS = ("input", "result")

#: The members an exported action must carry. ``bundle``, ``policy``,
#: ``approval`` and the two origins lists are optional because a run may have
#: no bundle, no policy and no approval to name; their absence is recorded as
#: a null member and an empty origin list, never as an omitted one.
REQUIRED_ACTION_MEMBERS = (
    "receipt_kind",
    "task",
    "component",
    "capability",
    "worker",
    "input",
    "result",
)

#: The environment routes for a receipt key, mirroring `attest`'s discipline:
#: `REVL_RECEIPT_KEY_FILE` is a path, `REVL_RECEIPT_KEY` is the secret bytes,
#: and neither is ever hardcoded.
KEY_ENV = "REVL_RECEIPT_KEY"
KEY_FILE_ENV = "REVL_RECEIPT_KEY_FILE"

#: The one member of a receipt the MAC does not cover: itself.
SIGNATURE_FIELD = "signature"

#: The members a receipt body must contain, exactly. The list is explicit so a
#: verifier refuses a body shape it does not implement rather than reading a
#: future member as decoration; it is also what keeps an added member visible.
RECEIPT_BODY_MEMBERS = (
    "kind",
    "version",
    "hash_alg",
    "sign_alg",
    "key_id",
    "session",
    "seq",
    "prev",
    "receipt_kind",
    "task",
    "component",
    "bundle",
    "policy",
    "capability",
    "approval",
    "worker",
    "session_verdict",
    "input",
    "input_origins",
    "result",
    "result_origins",
    "issued_at",
    "digest",
)

#: The members a chain envelope must contain, exactly. `digests` binds the
#: receipt ORDER and `count`/`head` bind the END, so truncation and reordering
#: are visible from the envelope signature alone; the per-receipt `prev` links
#: then make the same tampering visible even to a reader who drops the
#: envelope signature check.
CHAIN_MEMBERS = (
    "kind",
    "version",
    "hash_alg",
    "sign_alg",
    "key_id",
    "session",
    "session_verdict",
    "salt",
    "count",
    "head",
    "digests",
)

#: The refusal links a caller branches on. One per check that can fire.
VERDICT_LINKS = (
    "envelope",
    "signature",
    "body",
    "commitment",
    "key-unknown",
    "chain-signature",
    "chain-order",
    "chain-link",
    "chain-head",
)

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_HEX16 = re.compile(r"\A[0-9a-f]{16}\Z")


@dataclass(frozen=True)
class Verdict:
    """The result of :func:`verify_receipt` or :func:`verify_chain`.

    ``ok`` is true only after every check passed. ``link`` is ``""`` on a pass
    and one of :data:`VERDICT_LINKS` on a refusal, so a caller branches on the
    check that fired rather than on the wording.
    """

    ok: bool
    link: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if self.link and self.link not in VERDICT_LINKS:
            raise ValueError(
                f"{self.link!r} is not a verdict link; the vocabulary is "
                f"{list(VERDICT_LINKS)}")

    def __bool__(self) -> bool:
        return self.ok


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------

def _require_key(key: Any, what: str = "a receipt key") -> bytes:
    """The key as bytes, or a refusal: an empty or non-bytes key would sign (or
    verify) with a MAC anyone can compute, so no entry point accepts one."""
    if not isinstance(key, (bytes, bytearray)) or not key:
        raise ValueError(f"{what} must be non-empty bytes")
    return bytes(key)


def key_id(key: bytes) -> str:
    """A non-secret fingerprint of a signing key.

    The same fingerprint `revl.attest` records (delegated rather than
    re-derived): a verifier that keeps one key-id ledger for attestations and
    receipts should not meet two spellings of "which key".
    """
    return attest.key_id(bytes(key))


def digest(value: Any) -> str:
    """Hex SHA-256 over the canonical spelling of ``value``.

    The canonicalization is `revl.attest._canonical_bytes`, the one byte
    spelling every other signed record in the tree uses. Raises
    :class:`~revl.attest.NotCanonicalizable` for a value with no canonical
    spelling, so a caller with an error channel can refuse instead of
    crashing.
    """
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _suppression_reason(origins: Iterable[str]) -> Optional[str]:
    """The reason a commitment over ``origins`` must be suppressed, or None.

    `secret` wins over `confidential` when both are present, so the answer is a
    function of the SET of origins and not of the order a caller supplied.
    """
    origin_set = set(origins)
    if "secret" in origin_set:
        return "secret-origin"
    if "confidential" in origin_set:
        return "confidential-origin"
    return None


def _commitment_value(value: Any, salt: str, label: str) -> str:
    """The salted digest for one commitment. Private: the salt spelling is
    validated once by :func:`commitment`."""
    material = (
        COMMITMENT_DOMAIN
        + b"\x00"
        + bytes.fromhex(salt)
        + b"\x00"
        + label.encode("utf-8")
        + b"\x00"
        + _canonical_bytes(value)
    )
    return hashlib.sha256(material).hexdigest()


def commitment(value: Any, origins: Iterable[str] = (), *,
               salt: str, label: str) -> dict:
    """Build the commitment object for ``value`` under ``origins``.

    A value whose origins include a disclosure class produces
    ``{"mode": "suppressed", "value": None, "reason": <which-origin>}`` and
    never a digest. Any other value produces
    ``{"mode": "salted", "value": <64-hex>, "reason": None}``.

    ``salt`` is the owning chain's 64-hex salt and ``label`` is ``input`` or
    ``result``. Both are part of the digest, so a commitment moved between the
    two members or between two chains no longer checks out.
    """
    if not isinstance(salt, str) or not _HEX64.match(salt):
        raise ValueError("a commitment salt must be 64 lowercase hex characters")
    if label not in COMMITMENT_LABELS:
        raise ValueError(f"a commitment label must be one of {list(COMMITMENT_LABELS)}")
    origin_list = list(origins)
    if any(not isinstance(item, str) for item in origin_list):
        raise ValueError("every origin must be a string")
    unknown = sorted(set(origin_list) - set(ORIGIN_CLASSES))
    if unknown:
        raise ValueError(
            f"unknown origin class(es) {unknown}; the lattice is {list(ORIGIN_CLASSES)}")
    reason = _suppression_reason(origin_list)
    if reason is not None:
        return {"mode": "suppressed", "value": None, "reason": reason}
    return {"mode": "salted", "value": _commitment_value(value, salt, label),
            "reason": None}


def _now() -> str:
    """The issuance instant, UTC, second precision, the same spelling
    `revl.model_evidence` stamps."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sign(body: Mapping[str, Any], key: bytes, domain: bytes,
          members: Optional[Sequence[str]] = None) -> str:
    """HMAC-SHA256 over the canonical spelling of the signed members,
    prefixed with the protocol domain.

    Like `revl.model_evidence._sign`, a receipt's input is derived from the
    record rather than from a hand-written field list, so a member added to the
    body is covered without anyone remembering to add it here. The chain
    envelope passes its closed member list instead, because the receipts it
    carries are each signed on their own and the envelope only needs to bind
    their digests and order.
    """
    if members is None:
        signed = {name: value for name, value in body.items()
                  if name != SIGNATURE_FIELD}
    else:
        signed = {name: body.get(name) for name in members}
    return hmac.new(bytes(key), domain + _canonical_bytes(signed),
                    hashlib.sha256).hexdigest()


def receipt_digest(receipt: Mapping[str, Any]) -> str:
    """The chain-link digest of one receipt: everything but its own ``digest``
    and ``signature``.

    This is the value the NEXT receipt names in its ``prev`` member. It covers
    every other member, including ``prev`` itself, so the links compose.
    """
    return digest({name: value for name, value in receipt.items()
                   if name not in ("digest", SIGNATURE_FIELD)})


# ---------------------------------------------------------------------------
# body validation
# ---------------------------------------------------------------------------

def _require_text(value: Any, name: str) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return f"{name} must be a non-empty string ({value!r})"
    return None


def _require_hex64_or_none(value: Any, name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not _HEX64.match(value):
        return f"{name} must be null or 64 lowercase hex characters ({value!r})"
    return None


def _check_origins(value: Any, name: str) -> Optional[str]:
    if not isinstance(value, list):
        return f"{name} must be a list of origin classes ({value!r})"
    if any(not isinstance(item, str) for item in value):
        return f"{name} must contain only origin-class strings"
    unknown = sorted(set(value) - set(ORIGIN_CLASSES))
    if unknown:
        return f"{name} names unknown origin class(es) {unknown}"
    if len(value) != len(set(value)):
        return f"{name} repeats an origin class ({value!r})"
    return None


def check_commitment(value: Any, origins: Any, label: str) -> Optional[str]:
    """Check one commitment against the declared origins of its value.

    This is the disclosure rule's enforcing half, and it is deliberately
    bidirectional: a disclosure origin REQUIRES a suppressed commitment with
    the matching reason, and any other origin requires a salted commitment
    with a digest. A sealed record therefore cannot leak a secret digest and
    cannot withhold an ordinary one without naming the disclosure class that
    made it do so.
    """
    if not isinstance(value, Mapping):
        return f"the {label} commitment is not an object ({value!r})"
    if any(not isinstance(name, str) for name in value):
        return f"the {label} commitment has a non-string member name"
    members = set(value)
    expected = {"mode", "value", "reason"}
    if members != expected:
        return (f"the {label} commitment has members {sorted(members)}, "
                f"expected exactly {sorted(expected)}")
    mode, reason = value.get("mode"), value.get("reason")
    if mode not in COMMITMENT_MODES:
        return (f"the {label} commitment mode is {mode!r}, "
                f"expected one of {list(COMMITMENT_MODES)}")
    origin_refusal = _check_origins(origins, f"{label}_origins")
    if origin_refusal is not None:
        return origin_refusal
    required = _suppression_reason(origins)
    if required is not None:
        if mode != "suppressed":
            return (f"the {label} value carries a {required.split('-')[0]} "
                    f"origin, so its commitment must be suppressed, not {mode}")
        if reason != required:
            return (f"the {label} commitment names reason {reason!r}, but its "
                    f"origins require {required!r}")
        if value.get("value") is not None:
            return (f"the suppressed {label} commitment carries a value digest")
        return None
    if mode != "salted":
        return (f"the {label} commitment is suppressed ({reason!r}) but its "
                f"origins carry no secret or confidential class")
    if not isinstance(value.get("value"), str) or not _HEX64.match(value["value"]):
        return (f"the salted {label} commitment value is not 64 lowercase hex "
                f"characters ({value.get('value')!r})")
    if reason is not None:
        return f"a salted {label} commitment carries a suppression reason"
    return None


def check_body(receipt: Any) -> Optional[str]:
    """Validate a receipt body, never raising on an unprintable member.

    The refusal messages interpolate untrusted members. A JSON integer above
    Python's 4300-digit conversion limit has no printable form, so building the
    message can itself raise `ValueError`. The contract here is a reason
    string, not an exception, so that case is a refusal.
    """
    try:
        return _check_body(receipt)
    except (ValueError, TypeError, RecursionError):
        return "the receipt cannot be read: a member has no bounded printable form"


def _check_body(receipt: Any) -> Optional[str]:
    """Validate a receipt body, ignoring the signature (which is checked
    first, by :func:`verify_receipt`).

    Returns a refusal string or None. Every member is required, every
    vocabulary is closed, and both directions of the disclosure rule are
    enforced here rather than in the writer, so a record that arrived from
    another signer is judged by the same rules that sealed a local one.
    """
    if not isinstance(receipt, Mapping):
        return f"not a receipt ({type(receipt).__name__})"
    if any(not isinstance(name, str) for name in receipt):
        return "a receipt's member names must be strings"
    members = set(receipt) - {SIGNATURE_FIELD}
    expected = set(RECEIPT_BODY_MEMBERS)
    if members != expected:
        missing = sorted(expected - members)
        extra = sorted(members - expected)
        return (f"the receipt body is not the {RECEIPT_VERSION} shape "
                f"(missing {missing}, extra {extra})")
    envelope = (
        ("kind", RECEIPT_KIND),
        ("version", RECEIPT_VERSION),
        ("hash_alg", HASH_ALG),
        ("sign_alg", SIGN_ALG),
    )
    for name, wanted in envelope:
        if receipt.get(name) != wanted:
            return f"envelope refused: {name} is {receipt.get(name)!r}, expected {wanted!r}"
    kid = receipt.get("key_id")
    if not isinstance(kid, str) or not _HEX16.match(kid):
        return f"envelope refused: key_id is not a key fingerprint ({kid!r})"
    for name in ("session", "task", "component", "capability", "worker"):
        refusal = _require_text(receipt.get(name), name)
        if refusal is not None:
            return refusal
    refusal = _require_text(receipt.get("issued_at"), "issued_at")
    if refusal is not None:
        return refusal
    for name in ("bundle", "policy"):
        refusal = _require_hex64_or_none(receipt.get(name), name)
        if refusal is not None:
            return refusal
    seq = receipt.get("seq")
    if type(seq) is not int or seq < 0:
        return f"seq must be a non-negative integer ({seq!r})"
    prev = receipt.get("prev")
    if prev is not None and (not isinstance(prev, str) or not _HEX64.match(prev)):
        return f"prev must be null or a receipt digest ({prev!r})"
    if receipt.get("receipt_kind") not in RECEIPT_KINDS:
        return (f"receipt_kind is {receipt.get('receipt_kind')!r}, "
                f"expected one of {list(RECEIPT_KINDS)}")
    if receipt.get("session_verdict") not in SESSION_VERDICTS:
        return (f"session_verdict is {receipt.get('session_verdict')!r}, "
                f"expected one of {list(SESSION_VERDICTS)}")
    approval = receipt.get("approval")
    if approval is not None:
        if not isinstance(approval, Mapping):
            return f"approval must be null or an object ({approval!r})"
        if any(not isinstance(name, str) for name in approval):
            return "approval's member names must be strings"
        if set(approval) != {"request_id", "use"}:
            return (f"approval has members {sorted(approval)}, expected exactly "
                    "['request_id', 'use']")
        refusal = _require_text(approval.get("request_id"), "approval.request_id")
        if refusal is not None:
            return refusal
        use = approval.get("use")
        if use is not None and (not isinstance(use, str) or not use):
            return f"approval.use must be null or a non-empty string ({use!r})"
    if receipt.get("receipt_kind") == "approval-use" and approval is None:
        return "an approval-use receipt must name the approval it used"
    for label in COMMITMENT_LABELS:
        origins = receipt.get(f"{label}_origins")
        refusal = check_commitment(receipt.get(label), origins, label)
        if refusal is not None:
            return refusal
    claimed_digest = receipt.get("digest")
    if not isinstance(claimed_digest, str) or not _HEX64.match(claimed_digest):
        return f"the receipt digest is not a digest ({claimed_digest!r})"
    try:
        recomputed = receipt_digest(receipt)
    except NotCanonicalizable as error:
        return str(error)
    if not hmac.compare_digest(recomputed, claimed_digest):
        return ("the receipt digest does not match its body: a member was "
                "added, removed or edited after the receipt was sealed")
    return None


# ---------------------------------------------------------------------------
# sealing
# ---------------------------------------------------------------------------

def _sealed_receipt(key: bytes, *, session: str, seq: int, prev: Optional[str],
                    receipt_kind: str, task: str, component: str,
                    bundle: Optional[str], policy: Optional[str],
                    capability: str, approval: Optional[dict], worker: str,
                    session_verdict: str, salt: str,
                    input: Any, input_origins: Sequence[str],
                    result: Any, result_origins: Sequence[str],
                    issued_at: str) -> dict:
    """Build, validate and sign one receipt. Raises :class:`RevlError` on a
    malformed body so an unaccountable receipt is never minted."""
    if isinstance(approval, Mapping):
        extra = sorted(str(name) for name in approval
                       if name not in ("request_id", "use"))
        if extra:
            raise RevlError(
                "<action-receipt>", 0,
                f"approval has member(s) {extra} beyond request_id and use; "
                "an approval is recorded whole, never thinned by dropping a "
                "member")
    body = {
        "kind": RECEIPT_KIND,
        "version": RECEIPT_VERSION,
        "hash_alg": HASH_ALG,
        "sign_alg": SIGN_ALG,
        "key_id": key_id(key),
        "session": session,
        "seq": seq,
        "prev": prev,
        "receipt_kind": receipt_kind,
        "task": task,
        "component": component,
        "bundle": bundle,
        "policy": policy,
        "capability": capability,
        "approval": None if approval is None else {
            "request_id": approval.get("request_id"),
            "use": approval.get("use"),
        },
        "worker": worker,
        "session_verdict": session_verdict,
        "input": commitment(input, input_origins, salt=salt, label="input"),
        "input_origins": sorted(set(input_origins)),
        "result": commitment(result, result_origins, salt=salt, label="result"),
        "result_origins": sorted(set(result_origins)),
        "issued_at": issued_at,
    }
    body["digest"] = receipt_digest(body)
    refusal = check_body(body)
    if refusal is not None:
        raise RevlError("<action-receipt>", 0, refusal)
    body[SIGNATURE_FIELD] = _sign(body, key, SIGN_DOMAIN)
    return body


def make_receipt(key: bytes, *, session: str, seq: int,
                 prev: Optional[str], receipt_kind: str, task: str,
                 component: str, bundle: Optional[str], policy: Optional[str],
                 capability: str, approval: Optional[Mapping], worker: str,
                 session_verdict: str, salt: str,
                 input: Any, result: Any,
                 input_origins: Sequence[str] = (),
                 result_origins: Sequence[str] = (),
                 issued_at: Optional[str] = None) -> dict:
    """One signed receipt, validated before it is signed.

    ``input``/``result`` are the RAW values; the commitments are built here
    from ``salt`` and the origins, so no caller can seal a commitment that
    disagrees with the origins it recorded. ``prev`` is the previous receipt's
    :func:`receipt_digest`, or None for the first receipt of a chain.
    """
    _require_key(key)
    for name, value, allowed in (
        ("receipt_kind", receipt_kind, RECEIPT_KINDS),
        ("session_verdict", session_verdict, SESSION_VERDICTS),
    ):
        if value not in allowed:
            raise ValueError(f"{name} must be one of {list(allowed)}, got {value!r}")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise ValueError(f"seq must be a non-negative integer, got {seq!r}")
    if prev is not None and (not isinstance(prev, str) or not _HEX64.match(prev)):
        raise ValueError(f"prev must be None or a receipt digest, got {prev!r}")
    if not isinstance(salt, str) or not _HEX64.match(salt):
        raise ValueError("salt must be 64 lowercase hex characters")
    return _sealed_receipt(
        bytes(key), session=session, seq=seq, prev=prev,
        receipt_kind=receipt_kind, task=task, component=component,
        bundle=bundle, policy=policy, capability=capability,
        approval=dict(approval) if approval is not None else None,
        worker=worker, session_verdict=session_verdict, salt=salt,
        input=input, input_origins=input_origins,
        result=result, result_origins=result_origins,
        issued_at=issued_at or _now())


def export_chain(key: bytes, *, session: str, actions: Iterable[Mapping],
                 session_verdict: str = "committed", salt: Optional[str] = None,
                 now: Optional[str] = None) -> dict:
    """Export a session's actions as one signed receipt chain.

    Each action is a mapping with the receipt's binding members::

        receipt_kind, task, component, bundle, policy, capability, approval,
        worker, input, input_origins, result, result_origins[, issued_at]

    ``session_verdict`` and the chain salt are session properties, so they are
    arguments here rather than per action. The chain is returned as a JSON
    document; the caller decides where to write it.
    """
    key = _require_key(key, "the chain key")
    if session_verdict not in SESSION_VERDICTS:
        raise ValueError(
            f"session_verdict must be one of {list(SESSION_VERDICTS)}, "
            f"got {session_verdict!r}")
    if salt is None:
        import os  # noqa: PLC0415 lazy; only the random-salt path needs it
        salt = os.urandom(32).hex()
    if not isinstance(salt, str) or not _HEX64.match(salt):
        raise ValueError("salt must be 64 lowercase hex characters")
    receipts: list[dict] = []
    prev = None
    for index, action in enumerate(actions):
        if not isinstance(action, Mapping):
            raise RevlError("<action-receipt>", 0,
                            f"action {index} is not an object")
        missing = sorted(set(REQUIRED_ACTION_MEMBERS) - set(action))
        if missing:
            raise RevlError(
                "<action-receipt>", 0,
                f"action {index} omits required member(s) {missing}; a receipt "
                "body is complete, never thinned by omission")
        # A chain may span a key rotation: the receipt carries the key_id of
        # whichever key signed it, and the verifier is given both. The chain
        # ENVELOPE is signed by the exporter's current key (`key`).
        receipt_key = action.get("key", key)
        receipts.append(make_receipt(
            receipt_key, session=session, seq=index, prev=prev,
            receipt_kind=action.get("receipt_kind"),
            task=action.get("task"),
            component=action.get("component"),
            bundle=action.get("bundle"),
            policy=action.get("policy"),
            capability=action.get("capability"),
            approval=action.get("approval"),
            worker=action.get("worker"),
            session_verdict=session_verdict,
            salt=salt,
            input=action.get("input"),
            input_origins=action.get("input_origins") or (),
            result=action.get("result"),
            result_origins=action.get("result_origins") or (),
            issued_at=action.get("issued_at") or now))
        prev = receipts[-1]["digest"]
    chain = _chain_envelope(key, session=session,
                            session_verdict=session_verdict, salt=salt,
                            receipts=receipts)
    return chain


def _chain_envelope(key: bytes, *, session: str, session_verdict: str,
                    salt: str, receipts: Sequence[Mapping]) -> dict:
    digests = [receipt["digest"] for receipt in receipts]
    chain = {
        "kind": CHAIN_KIND,
        "version": CHAIN_VERSION,
        "hash_alg": HASH_ALG,
        "sign_alg": SIGN_ALG,
        "key_id": key_id(key),
        "session": session,
        "session_verdict": session_verdict,
        "salt": salt,
        "count": len(receipts),
        "head": digests[-1] if digests else None,
        "digests": digests,
        "receipts": [dict(receipt) for receipt in receipts],
    }
    chain[SIGNATURE_FIELD] = _sign(chain, key, CHAIN_SIGN_DOMAIN,
                                   members=CHAIN_MEMBERS)
    return chain


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------

def _keyring(keys: Any) -> dict:
    """Normalize a single key, an iterable of keys or a ``key_id -> key``
    mapping into a keyring. A duplicate fingerprint with different bytes is a
    caller error, not a precedence question."""
    if isinstance(keys, (bytes, bytearray)):
        key = _require_key(keys, "a verification key")
        return {key_id(key): key}
    if isinstance(keys, Mapping):
        items = []
        for kid, key in dict(keys).items():
            key = _require_key(key, "a verification key")
            if kid != key_id(key):
                raise ValueError(
                    f"keyring id {str(kid)[:64]!r} is not the fingerprint of "
                    "the key it maps to")
            items.append((kid, key))
    elif isinstance(keys, Iterable):
        items = ((key_id(key), key) for key in
                 (_require_key(key, "a verification key") for key in keys))
    else:
        raise TypeError("keys must be bytes, an iterable of bytes or a mapping")
    out: dict = {}
    for kid, key in items:
        if kid in out and out[kid] != key:
            raise ValueError(f"two different keys share the fingerprint {kid}")
        out[kid] = key
    return out


def verify_receipt(receipt: Any, keys: Any) -> Verdict:
    """Check one sealed receipt against a key or key set. Fail-closed.

    See :func:`_verify_receipt` for the checks. This wrapper is what makes the
    module's contract hold against a hostile document: a refusal message that
    cannot be built (a huge int, a cycle, a value with no canonical spelling)
    is a refusal, never an exception escaping the verifier.
    """
    try:
        return _verify_receipt(receipt, keys)
    except (ValueError, TypeError, RecursionError):
        return Verdict(False, "envelope",
                       "the receipt cannot be read: a member has no bounded "
                       "printable form")


def _verify_receipt(receipt: Any, keys: Any) -> Verdict:
    """Check one sealed receipt against a key or key set. Fail-closed.

    Order: the record and its signature first, because a tampered member must
    be refused as tampered rather than as malformed; then the body, which the
    MAC does not make well-formed.
    """
    if not isinstance(receipt, Mapping):
        return Verdict(False, "envelope",
                       f"not a receipt ({type(receipt).__name__})")
    if SIGNATURE_FIELD not in receipt:
        return Verdict(False, "envelope", "the receipt carries no signature")
    claimed = receipt.get(SIGNATURE_FIELD)
    if not isinstance(claimed, str) or not _HEX64.match(claimed):
        return Verdict(False, "signature",
                       f"the signature is not an {SIGN_ALG} tag ({claimed!r})")
    try:
        ring = _keyring(keys)
    except (TypeError, ValueError) as error:
        return Verdict(False, "key-unknown", str(error))
    kid = receipt.get("key_id")
    if not isinstance(kid, str) or kid not in ring:
        return Verdict(False, "key-unknown",
                       f"the receipt names key {str(kid)[:64]!r}, which the "
                       "verifier was not given")
    try:
        expected = _sign(receipt, ring[kid], SIGN_DOMAIN)
    except NotCanonicalizable as error:
        return Verdict(False, "body", str(error))
    if not hmac.compare_digest(expected, claimed):
        return Verdict(False, "signature",
                       "the signature does not cover this receipt: a member "
                       "was added, removed or edited after it was sealed, or "
                       "it was sealed with another key")
    refusal = check_body(receipt)
    if refusal is not None:
        link = ("commitment" if "commitment" in refusal.lower() else "body")
        return Verdict(False, link, refusal)
    return Verdict(True)


def verify_chain(document: Any, keys: Any) -> Verdict:
    """Check a whole receipt chain against a key or key set. Fail-closed.

    See :func:`_verify_chain` for the checks. The wrapper is the same guard
    :func:`verify_receipt` has: a document whose refusal cannot be rendered is
    refused rather than raising, because a presented chain is untrusted.
    """
    try:
        return _verify_chain(document, keys)
    except (ValueError, TypeError, RecursionError):
        return Verdict(False, "envelope",
                       "the chain cannot be read: a member has no bounded "
                       "printable form")


def _verify_chain(document: Any, keys: Any) -> Verdict:
    """Check a whole receipt chain against a key or key set. Fail-closed.

    The checks, in order, are: the envelope and its signature; every receipt's
    signature and body; the order (contiguous ``seq``, one session, one
    session verdict, ``prev`` naming the predecessor's digest); and the end
    (``count``, ``digests`` and ``head`` agreeing with the receipts). Deleting
    a receipt, swapping two, altering one field or truncating the tail each
    fail a different link, and the reason names it.
    """
    if not isinstance(document, Mapping):
        return Verdict(False, "envelope",
                       f"not a chain document ({type(document).__name__})")
    if any(not isinstance(name, str) for name in document):
        return Verdict(False, "envelope",
                       "the chain envelope's member names must be strings")
    if document.get("kind") != CHAIN_KIND:
        return Verdict(False, "envelope",
                       f"not a {CHAIN_KIND} record: kind is "
                       f"{document.get('kind')!r}")
    if document.get("version") != CHAIN_VERSION:
        return Verdict(False, "envelope",
                       f"a {CHAIN_KIND} version this build does not implement: "
                       f"{document.get('version')!r}")
    members = set(document) - {SIGNATURE_FIELD}
    if members != set(CHAIN_MEMBERS) | {"receipts"}:
        missing = sorted((set(CHAIN_MEMBERS) | {"receipts"}) - members)
        extra = sorted(members - (set(CHAIN_MEMBERS) | {"receipts"}))
        return Verdict(False, "envelope",
                       f"the chain envelope is not the {CHAIN_VERSION} shape "
                       f"(missing {missing}, extra {extra})")
    for name, wanted in (("hash_alg", HASH_ALG), ("sign_alg", SIGN_ALG)):
        if document.get(name) != wanted:
            return Verdict(False, "envelope",
                           f"envelope refused: {name} is "
                           f"{document.get(name)!r}, expected {wanted!r}")
    try:
        ring = _keyring(keys)
    except (TypeError, ValueError) as error:
        return Verdict(False, "key-unknown", str(error))
    kid = document.get("key_id")
    if not isinstance(kid, str) or kid not in ring:
        return Verdict(False, "key-unknown",
                       f"the chain names key {str(kid)[:64]!r}, which the "
                       "verifier was not given")
    if SIGNATURE_FIELD not in document:
        return Verdict(False, "envelope", "the chain carries no signature")
    claimed = document.get(SIGNATURE_FIELD)
    if not isinstance(claimed, str) or not _HEX64.match(claimed):
        return Verdict(False, "chain-signature",
                       f"the chain signature is not an {SIGN_ALG} tag ({claimed!r})")
    try:
        expected = _sign(document, ring[kid], CHAIN_SIGN_DOMAIN,
                         members=CHAIN_MEMBERS)
    except NotCanonicalizable as error:
        return Verdict(False, "chain-signature", str(error))
    if not hmac.compare_digest(expected, claimed):
        return Verdict(False, "chain-signature",
                       "the chain signature does not cover this envelope: a "
                       "member was added, removed or edited after export, or "
                       "it was sealed with another key")
    session = document.get("session")
    if not isinstance(session, str) or not session:
        return Verdict(False, "envelope", "the chain names no session")
    verdict_value = document.get("session_verdict")
    if verdict_value not in SESSION_VERDICTS:
        return Verdict(False, "envelope",
                       f"session_verdict is {verdict_value!r}, expected one of "
                       f"{list(SESSION_VERDICTS)}")
    salt = document.get("salt")
    if not isinstance(salt, str) or not _HEX64.match(salt):
        return Verdict(False, "envelope",
                       f"the chain salt is not 64 lowercase hex characters ({salt!r})")
    digests = document.get("digests")
    if not isinstance(digests, list) or any(
            not isinstance(item, str) or not _HEX64.match(item)
            for item in digests):
        return Verdict(False, "envelope", "the chain digests are not digests")
    count = document.get("count")
    if type(count) is not int or count < 0:
        return Verdict(False, "envelope",
                       f"the chain count is not a non-negative integer ({count!r})")
    receipts = document.get("receipts")
    if not isinstance(receipts, list):
        return Verdict(False, "envelope", "the chain receipts are not a list")
    if len(receipts) != count or len(digests) != count:
        return Verdict(False, "chain-order",
                       f"the chain claims {count} receipt(s) but carries "
                       f"{len(receipts)} receipt(s) and {len(digests)} digest(s)")
    previous = None
    for index, receipt in enumerate(receipts):
        checked = verify_receipt(receipt, ring)
        if not checked.ok:
            return Verdict(False, checked.link,
                           f"receipt {index}: {checked.reason}")
        if receipt.get("seq") != index:
            return Verdict(False, "chain-order",
                           f"receipt {index} carries seq {receipt.get('seq')!r}")
        if receipt.get("session") != session:
            return Verdict(False, "chain-order",
                           f"receipt {index} belongs to session "
                           f"{receipt.get('session')!r}, not {session!r}")
        if receipt.get("session_verdict") != verdict_value:
            return Verdict(False, "chain-order",
                           f"receipt {index} carries verdict "
                           f"{receipt.get('session_verdict')!r}, but the chain "
                           f"carries {verdict_value!r}")
        if receipt.get("prev") != previous:
            return Verdict(False, "chain-link",
                           f"receipt {index} names prev {receipt.get('prev')!r}, "
                           f"which is not receipt {index - 1}'s digest "
                           f"{previous!r}")
        if receipt.get("digest") != digests[index]:
            return Verdict(False, "chain-order",
                           f"receipt {index} has digest {receipt.get('digest')!r}, "
                           f"but the envelope lists {digests[index]!r}")
        previous = receipt["digest"]
    if document.get("head") != (digests[-1] if digests else None):
        return Verdict(False, "chain-head",
                       f"the chain head is {document.get('head')!r}, which is "
                       "not the last receipt's digest")
    return Verdict(True)


# ---------------------------------------------------------------------------
# keys
# ---------------------------------------------------------------------------

def resolve_keys(paths: Optional[Sequence[str]], *, env=None,
                 require: bool = True) -> list[bytes]:
    """Resolve verification/signing keys from explicit paths, then
    ``REVL_RECEIPT_KEY_FILE``, then ``REVL_RECEIPT_KEY``.

    No hardcoded default: a missing key is an error on the issue path and a
    refusal on the verify path, never a key the tree assumed.
    """
    if env is None:
        import os  # noqa: PLC0415 lazy so the module has no import side effect
        env = os.environ
    keys: list[bytes] = [attest.load_key(path) for path in (paths or [])]
    if not keys:
        file_env = env.get(KEY_FILE_ENV)
        if file_env:
            keys.append(attest.load_key(file_env))
        inline = env.get(KEY_ENV)
        if inline:
            keys.append(inline.encode("utf-8"))
    if not keys and require:
        raise RevlError(
            "<receipts>", 0,
            f"no receipt key: pass --key PATH, or set {KEY_FILE_ENV} (a key "
            f"file) or {KEY_ENV} (the secret)")
    return keys


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def render_verify(verdict: Verdict, document: Mapping) -> str:
    """The human-readable line(s) for a chain verification."""
    glyph = "VALID" if verdict.ok else "INVALID"
    lines = [f"receipt chain: {glyph}"]
    if verdict.ok:
        lines.append(f"  session:  {document.get('session', '?')}  "
                     f"({document.get('session_verdict', '?')})")
        lines.append(f"  receipts: {document.get('count', '?')}")
        lines.append(f"  head:     {document.get('head') or '(empty chain)'}")
        lines.append(f"  signed:   key {document.get('key_id', '?')}")
    else:
        lines.append(f"  refused:  {verdict.reason}")
    return "\n".join(lines)


__all__ = [
    "CHAIN_KIND",
    "CHAIN_VERSION",
    "COMMITMENT_LABELS",
    "COMMITMENT_MODES",
    "DISCLOSURE_ORIGINS",
    "HASH_ALG",
    "KEY_ENV",
    "KEY_FILE_ENV",
    "ORIGIN_CLASSES",
    "RECEIPT_BODY_MEMBERS",
    "RECEIPT_KINDS",
    "RECEIPT_KIND",
    "RECEIPT_VERSION",
    "SESSION_VERDICTS",
    "SIGN_ALG",
    "SUPPRESSION_REASONS",
    "VERDICT_LINKS",
    "Verdict",
    "check_body",
    "check_commitment",
    "commitment",
    "digest",
    "export_chain",
    "key_id",
    "make_receipt",
    "receipt_digest",
    "render_verify",
    "resolve_keys",
    "verify_chain",
    "verify_receipt",
]

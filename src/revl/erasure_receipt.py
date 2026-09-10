"""The signed erasure receipt: roadmap item 472's erasure half.

`revl erase-report --realm R` (item 29, src/revl/erase_report.py) measures what
a realm's erasure reaches, and it measures it well: the in-process state gone,
every boundary crossing the realm's components make with its compensation
state, the other realms provably untouched. What a report cannot do is travel.
A report is a document a reader decides to believe.

A receipt is a document a reader can CHECK. This module signs the report:

  * the receipt carries the canonical hash of the report it was issued over, so
    an auditor holding the receipt and a report can tell whether the report is
    the one that was measured, and a report edited after the fact stops
    verifying;
  * the receipt NAMES every replica the erasure reaches, one row per crossing
    token the report itself enumerated. Nothing here enumerates a replica the
    report did not enumerate, and nothing here invents a recipient: the rows
    come from the G8 boundary surface (`query.Composition`), the same surface
    `revl audit` prints and the report folds;
  * each row carries the disposition the report already assigned it plus the
    host slot, when one exists, through which that crossing can be reversed or
    offset. A `revertible` row names its registered inverse, a `compensated` or
    `unresolved` row names its declared `compensate` callee, and a `bare` row
    names nothing because the declaration names nothing.

HONEST SCOPE. A receipt is evidence about a MEASUREMENT, not about the world.
Signing the report does not erase anything, and it does not extend the report's
reach: a replica made outside revl's boundary never became a crossing, so it is
not a row, and no signature can name it. The item's own wording draws that line
("it cannot attest to copies made outside its boundary"); this module makes the
line a member of the signed document rather than a hope.

THREE SIGNED PROTOCOLS, THREE DOMAINS, ONE KEY RULE. `attest` (item 127) signs
a gate verdict, `deploy` (item 478) MACs an admission receipt, and this signs an
erasure report. They share the canonical serialization and the HMAC-SHA256
construction because a second signing story is a second thing to get wrong, and
they carry distinct domain tags because without one a receipt verified as an
attestation. "Shared" is meant literally here: this module calls
`attest._canonical_bytes` and `attest.load_key` rather than re-deriving either,
so the byte spelling and the key file rule have one implementation. The key is
resolved from `--receipt-key`, `REVL_ERASURE_KEY_FILE`, then `REVL_ERASURE_KEY`,
never hardcoded: a missing key is an error, so a receipt is never signed with a
secret the tree assumed.

WHAT THE RETENTION HALF IS NOT. Item 472 also asks for a `Retained[T]` type
whose value past its retention deadline is refused at a persistence sink. That
half is NOT implemented here, and this module does not pretend otherwise.
docs/design/472-retention-erasure-receipts.md records the measurement behind
that decision: the tree has no persistence sink the type system owns, no
residence or legal-hold vocabulary, and no runtime age fact a checker could
refuse on, so the deadline half is a language change with named prerequisites
rather than a slice that could be landed honestly.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Mapping, Optional

from . import attest
from .errors import RevlError
from .query import Composition
from .resources import _callee_name

# Receipt identity, in the spirit of `erase_report`: a versioned,
# self-describing artifact. Bump MINOR for an additive change, MAJOR for a
# breaking one (a removed or re-shaped member).
RECEIPT_VERSION = "1.0"
RECEIPT_KIND = "revl.erasure-receipt"

# The MAC is taken over this tag ++ the canonical body bytes. The tag is not
# decoration: `attest._sign` and `deploy._receipt_mac` MAC the same shape of
# document with the same construction, so WITHOUT a distinct domain an erasure
# receipt verified as an attestation and an attestation verified as a receipt.
# Three signed protocols, three domains.
RECEIPT_DOMAIN = b"revl.erasure-receipt/v1\x00"
SIGNATURE_FIELD = "signature"
HASH_ALG = "sha256"
SIGN_ALG = "hmac-sha256"

KEY_ENV = "REVL_ERASURE_KEY"
KEY_FILE_ENV = "REVL_ERASURE_KEY_FILE"
KEY_ID_DOMAIN = b"revl-erasure-keyid\x00"

# The statuses a BOUNDARY replica row can carry. They are the erase report's own
# vocabulary, not a new one: a receipt that renamed the report's states would be
# a second account of the same fact.
DISPOSITIONS = ("revertible", "compensated", "unresolved", "bare")

# The statuses the realm's OWN in-process row can carry. The R4 no-residue
# proof is a tri-state and the row is too, one disposition per reading:
# `reclaimed` when the proof stands, `residue` when the proof RAN and left
# something behind (the reading `revl erase-report` exits 1 on), and `unproven`
# when no proof was taken, which is the honest reading of "we did not measure
# it" and never of "it is still there". `proven: False` and `proven: null` are
# opposite facts, so signing them as one word would let a receipt be re-read as
# the other; `_envelope` checks the disposition against the row's own evidence.
# Kept APART from DISPOSITIONS so the envelope check for a boundary row cannot
# be satisfied by an in-process state, and vice versa.
IN_PROCESS_DISPOSITIONS = ("reclaimed", "unproven", "residue")

# Every disposition a replica row of either shape can carry, in render order.
ALL_DISPOSITIONS = DISPOSITIONS + IN_PROCESS_DISPOSITIONS

# The header the receipt states about itself, mirroring `erase_report.
# HONEST_SCOPE`: the difference between "the data is gone everywhere" (false)
# and "here is every replica this system can see, what state it is in, and who
# signed that measurement" (what this artifact establishes).
SCOPE = {
    "title": "What this receipt proves, and what it does not",
    "proves": [
        "the measurement is unaltered: the receipt carries the canonical "
        "sha256 of the erase report it was issued over, so a report changed "
        "after issue stops verifying against its own receipt.",
        "every replica the compiler can see is named: one row per boundary "
        "crossing the realm's components make, off the same G8 surface `revl "
        "audit` prints, each with the disposition the report assigned it.",
        "the receipt was issued by the holder of the signing key: an "
        "HMAC-SHA256 over the canonical body, tagged with this protocol's own "
        "domain, and attributable to a key fingerprint.",
    ],
    "doesNotProve": [
        "that anything was erased. A crossing names the boundary a value left "
        "through; what the other side did with it is not a fact this system "
        "holds. The in-process row is the only erasure claim here, and it reads "
        "`reclaimed` only when the R4 no-residue proof stands, `residue` when "
        "the proof ran and left something behind, and `unproven` when no proof "
        "was taken.",
        "copies the system cannot see. A replica made outside revl's boundary "
        "was never a crossing, so it is never a row. The scope of any erasure "
        "receipt is the replicas the issuing system knows about.",
        "that an offset landed. `compensated` is an offset attached and "
        "reported landed, `unresolved` is an offset owed that did not land, and "
        "`bare` is a crossing nothing was done about. Compensation is not "
        "inversion (paper §6.1): a compensated row still left the system.",
        "retention deadlines. No type in this tree carries one, so no receipt "
        "records one (docs/design/472-retention-erasure-receipts.md).",
    ],
    "reference": "docs/erase-report.md; docs/design/472-retention-erasure-receipts.md",
}

# Which boundary bucket a crossing came out of, and the report member it is
# read from. The receipt never invents a bucket: these are the report's own.
_BUCKETS = (
    ("witnessed", "witnessed"),
    ("emissions", "emission"),
    ("externs", "extern"),
    ("widenings", "widening"),
)


# ------------------------------------------------------------------- the key

def load_key(path) -> bytes:
    """Read a signing key from a file.

    `attest.load_key`'s rule, delegated to rather than restated: the key is the
    file's bytes with one trailing newline stripped (so a key written with
    `echo` round-trips), and nothing else is assumed. docs/revl-attest.md is the
    rule; sharing the implementation is what makes it one rule instead of two
    readings of one rule. Two implementations of a key file meant the same
    `cat`-created file was two different keys, so the same receipt MACed and
    fingerprinted twice depending on which protocol resolved it."""
    return attest.load_key(str(path))


def key_from_env(env=None) -> bool:
    """Whether an operator has exported this protocol's signing key. Asking for
    a receipt is explicit: `--receipt-key PATH`, or one of the two environment
    variables, and never anything else. A run that sets neither gets the report
    it always got, byte-identical."""
    if env is None:
        import os  # noqa: PLC0415

        env = os.environ
    return bool(env.get(KEY_FILE_ENV) or env.get(KEY_ENV))


def resolve_key(key_path: str | None, *, env=None) -> bytes:
    """Resolve the signing key from, in order: an explicit `--receipt-key`
    path, `REVL_ERASURE_KEY_FILE` (a path), then `REVL_ERASURE_KEY` (the secret
    bytes directly). Never a hardcoded default: a missing key is an error, so a
    receipt is never signed with a secret the tree assumed."""
    if env is None:
        import os  # noqa: PLC0415

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
        "<erase-receipt>", 0,
        f"no signing key: pass --receipt-key PATH, or set {KEY_FILE_ENV} (a "
        f"key file) or {KEY_ENV} (the secret)")


def key_id(key: bytes) -> str:
    """A non-secret fingerprint of the signing key, so a verifier can tell
    WHICH key it needs without the key being present. One-way and stable, and
    tagged with its own domain so an erasure key fingerprint and an attestation
    key fingerprint of the same bytes are different strings rather than
    accidentally equal."""
    return hashlib.sha256(KEY_ID_DOMAIN + bytes(key)).hexdigest()[:16]


# -------------------------------------------------------- the replica surface

def _inverse(index: Optional[Composition], name: str,
             disposition: str) -> Optional[str]:
    """The named host slot that closes or offsets this crossing, or None.

    A `revertible` (witnessed, class (a)) row is closed by the extern's `undo`
    clause; a `compensated` or `unresolved` row is offset by its `compensate`
    clause. Read through `resources._callee_name`, the same accessor
    `resources.closing_ops` uses for the O1 double-close audit, so a receipt
    cannot name an inverse the O1 audit does not recognize."""
    if index is None:
        return None
    entry = index.externs.get(name) or {}
    slot = "undo" if disposition == "revertible" else "compensate"
    return _callee_name(entry.get(slot))


def _row(bucket: str, entry: Mapping, unresolved: set,
         index: Optional[Composition], boundary: str) -> dict:
    """One replica row, derived from one report crossing. `disposition` is the
    report's own state for this token, read from the bucket it sits in and, for
    an irreversible crossing, from whether its token is in the report's
    `unresolvedTokens` and whether a `compensate` clause is attached."""
    token = entry.get("token")
    if bucket == "witnessed":
        disposition = "revertible"
    elif token in unresolved:
        disposition = "unresolved"
    elif entry.get("compensated"):
        disposition = "compensated"
    else:
        disposition = "bare"
    name = entry.get("name")
    row = {
        "replica": token,
        "boundary": boundary,
        "component": entry.get("component"),
        "capabilities": list(entry.get("capabilities") or []),
        "disposition": disposition,
        "inverse": _inverse(index, name, disposition) if name else None,
    }
    if entry.get("label"):
        row["label"] = entry["label"]
    return row


def replicas(report: Mapping, ir: Optional[dict] = None) -> list[dict]:
    """Every replica the report enumerated, as signed-ready rows.

    `ir` is the composition the report was built from, used only to name the
    declared inverse of a revertible or compensated crossing. Omitted, the rows
    still carry the report's dispositions and name no inverse: the receipt is
    less dated than it could be, never wrong."""
    crossings = (report or {}).get("boundaryCrossings") or {}
    unresolved = set(crossings.get("unresolvedTokens") or [])
    index = Composition(ir) if ir is not None else None
    out: list[dict] = []
    for bucket, boundary in _BUCKETS:
        for entry in crossings.get(bucket) or []:
            out.append(_row(bucket, entry, unresolved, index, boundary))
    out.sort(key=lambda r: (r["replica"] or ""))
    return out


def in_process(report: Mapping) -> dict:
    """The realm's own copy: the in-process state the R4 no-residue proof
    covers. It is a replica like any other and gets a row like any other, one
    disposition per reading of the proof: `reclaimed` when the proof stands,
    `residue` when the proof ran and left something behind, `unproven` when it
    was not taken.

    The disposition is DERIVED from the row's own signed members (`proven`, and
    the reason or the failed checks it carries), so "it is still there" and
    "nobody looked" are different signed documents rather than one word two
    readers have to agree about. `_envelope` re-checks the pair, so a body
    claiming `reclaimed` over `proven: False` is refused even if it is MACed."""
    state = (report or {}).get("inProcessStateGone") or {}
    proof = state.get("noResidueProof") or {}
    proven = state.get("proven")
    if proven is True:
        disposition = "reclaimed"
    elif proven is False:
        disposition = "residue"
    else:
        disposition = "unproven"
    return {
        "replica": f"memory://{(report or {}).get('realm')}",
        "boundary": "in-process",
        "component": None,
        "capabilities": [],
        "disposition": disposition,
        "inverse": None,
        "provisionsErased": list(state.get("provisionsErased") or []),
        "proven": proven,
        # why it is not `reclaimed`, inside the signed body: the proof's own
        # availability and reason, and the checks that did not hold.
        "available": proof.get("available"),
        "reason": proof.get("reason"),
        "failedChecks": sorted(
            name for name, held in (proof.get("checks") or {}).items()
            if not held),
    }


def _hashable_report(report: Mapping) -> dict:
    """The report a receipt binds: the report as it stood BEFORE the receipt was
    attached. The receipt rides inside the report it signs (append-only, like
    every other member this toolchain adds), so binding the whole document
    would make the binding self-referential and unverifiable."""
    return {k: v for k, v in (report or {}).items()
            if k not in ("receipt", "erasureReceipt")}


def report_hash(report: Mapping) -> str:
    """The canonical sha256 of the report a receipt binds."""
    return attest.canonical_hash(_hashable_report(report))


# ----------------------------------------------------------------- the receipt

def build_body(report: Mapping, ir: Optional[dict] = None, *,
               now=None, signer: Optional[str] = None,
               key: Optional[bytes] = None) -> dict:
    """The unsigned receipt body: every field except the signature.

    `make_receipt` signs this, and `verify_receipt` re-MACs exactly the members
    it receives (everything but `signature`), so any altered, dropped or added
    member breaks the signature. The whole record is committed, not a chosen
    subset."""
    rows = replicas(report, ir)
    counts = {name: 0 for name in ALL_DISPOSITIONS}
    for row in rows:
        counts[row["disposition"]] = counts.get(row["disposition"], 0) + 1
    realm_row = in_process(report)
    counts[realm_row["disposition"]] = counts.get(realm_row["disposition"], 0) + 1
    summary = {
        "replicas": len(rows) + 1,
        "boundaryReplicas": len(rows),
        "outsideBoundary": sum(1 for r in rows if r["boundary"] != "in-process"),
        "namedInverses": sum(1 for r in rows if r["inverse"]),
        "byDisposition": counts,
        "otherRealmsUntouched":
            bool(((report or {}).get("otherRealmsUntouched") or {})
                 .get("untouched")),
    }
    return {
        "kind": RECEIPT_KIND,
        "version": RECEIPT_VERSION,
        "hash_alg": HASH_ALG,
        "sign_alg": SIGN_ALG,
        "realm": (report or {}).get("realm"),
        "issued_at": attest._now_iso(now),
        "signer": signer,
        "key_id": key_id(key) if key else None,
        "report": {
            "kind": (report or {}).get("kind"),
            "schema_version": (report or {}).get("schema_version"),
            "hash": report_hash(report),
        },
        "inProcess": realm_row,
        "replicas": rows,
        "summary": summary,
        "scope": SCOPE,
    }


def _mac(body: Mapping, key: bytes) -> str:
    """The receipt MAC: domain-tagged HMAC-SHA256 over the canonical body bytes
    (`body` is the receipt with its `signature` member removed).

    The bytes are `attest._canonical_bytes`'s, the same construction
    `attest._sign` and `deploy._receipt_mac` MAC over, deferred to rather than
    restated: a verifier who canonicalizes the way docs/revl-attest.md
    documents recomputes exactly these bytes, which is only true while there is
    one implementation of the spelling. In particular non-ASCII text is emitted
    as UTF-8 rather than `\\u`-escaped, so a receipt signed by a name like
    "Jose Muller" still verifies outside this module.

    A body with no canonical byte spelling raises
    `attest.NotCanonicalizable`, which `verify_receipt` turns into a refusal
    rather than a crash, the same contract `deploy._receipt_mac` keeps."""
    return hmac.new(bytes(key), RECEIPT_DOMAIN + attest._canonical_bytes(
        {k: v for k, v in body.items() if k != SIGNATURE_FIELD}),
        hashlib.sha256).hexdigest()


def make_receipt(report: Mapping, key: bytes, *, ir: Optional[dict] = None,
                 now=None, signer: Optional[str] = None) -> dict:
    """Build the signed erasure receipt for one realm's erase report.

    Pure and deterministic given `now`: the same (report, ir, key, now, signer)
    always produces byte-identical output, which is what makes the round trip
    testable and the receipt reproducible from the evidence it summarises.

    An unknown realm is never signed. `build_report` returns a document whose
    `ok` is False for a realm the composition does not name, and signing one
    would put a signature on an erasure of nothing."""
    if not isinstance(key, (bytes, bytearray)) or not key:
        raise RevlError("<erase-receipt>", 0,
                        "the signing key must be non-empty bytes")
    if not (report or {}).get("ok"):
        raise RevlError(
            "<erase-receipt>", 0,
            "there is no completed erase report to sign: "
            f"{((report or {}).get('error')) or 'the report is not ok'}. A "
            "receipt is evidence about a measurement, and this realm was not "
            "measured")
    body = build_body(report, ir, now=now, signer=signer, key=bytes(key))
    return {**body, SIGNATURE_FIELD: _mac(body, bytes(key))}


def _envelope(receipt: Mapping) -> str:
    """The envelope check: is this record even an erasure receipt of the shape
    this verifier accepts? Returns a refusal reason, or `""` when well formed.

    A MAC proves authorship. It does not prove that what was authored means
    what the reader assumes, so every member whose value carries a fixed
    meaning is checked here: a `revl.attestation` or a `revl.deploy.receipt`
    presented as an erasure receipt is a mislabel, and a record whose
    dispositions are not this protocol's vocabulary is refused rather than
    printed VALID."""
    def reason(member, expected, found):
        return (f"envelope refused: {member} is {found!r}, expected "
                f"{expected!r}")

    if not isinstance(receipt, Mapping):
        return "envelope refused: the receipt is not a document"
    if receipt.get("kind") != RECEIPT_KIND:
        return reason("kind", RECEIPT_KIND, receipt.get("kind"))
    if receipt.get("version") != RECEIPT_VERSION:
        return reason("version", RECEIPT_VERSION, receipt.get("version"))
    if receipt.get("hash_alg") != HASH_ALG:
        return reason("hash_alg", HASH_ALG, receipt.get("hash_alg"))
    if receipt.get("sign_alg") != SIGN_ALG:
        return reason("sign_alg", SIGN_ALG, receipt.get("sign_alg"))
    if not isinstance(receipt.get("realm"), str) or not receipt.get("realm"):
        return reason("realm", "a non-empty realm name", receipt.get("realm"))
    if not isinstance(receipt.get(SIGNATURE_FIELD), str):
        return "envelope refused: no signature to verify"
    report = receipt.get("report")
    if not isinstance(report, Mapping) or not isinstance(report.get("hash"), str):
        return reason("report.hash", "the sha256 of the signed report",
                      (report or {}).get("hash") if isinstance(report, Mapping)
                      else report)
    rows = receipt.get("replicas")
    if not isinstance(rows, list):
        return reason("replicas", "a list of replica rows", rows)
    for row in rows:
        if not isinstance(row, Mapping):
            return reason("replicas[]", "a replica row", row)
        if row.get("disposition") not in DISPOSITIONS:
            return reason("replicas[].disposition", DISPOSITIONS,
                          row.get("disposition"))
    state = receipt.get("inProcess")
    if not isinstance(state, Mapping):
        return reason("inProcess", "the realm's own in-process replica row",
                      state)
    if state.get("disposition") not in IN_PROCESS_DISPOSITIONS:
        return reason("inProcess.disposition", IN_PROCESS_DISPOSITIONS,
                      state.get("disposition"))
    # The disposition is a summary of the row's OWN evidence, so the two are
    # checked against each other: the mapping from the proof reading to the
    # disposition is total, so `reclaimed` is not a free choice a forger can
    # pair with a proof that was never taken, and `proven` has to BE one of the
    # three readings. A row remembered as `unproven` while its own `proven` says
    # `false` is refused here rather than printed VALID.
    proven = state.get("proven")
    expected = ("reclaimed" if proven is True else
                "residue" if proven is False else
                "unproven" if proven is None else None)
    if expected is None:
        return reason("inProcess.proven", "true, false or null", proven)
    if state.get("disposition") != expected:
        return reason("inProcess.disposition", expected,
                      state.get("disposition"))
    return ""


def verify_receipt(receipt: Mapping, key: bytes, *,
                   report: Mapping | None = None) -> tuple[bool, str]:
    """Verify an erasure receipt against the signing key. Returns
    `(ok, reason)`; `reason` is `""` when `ok`.

    Four checks, in order, so the cheapest refusal wins and a caller can tell
    a wrong key from a tampered body from a stale report:

      * the envelope: kind, version, algorithms, realm, signature presence and
        the disposition vocabulary (a MAC on a mislabelled document is still a
        mislabelled document);
      * the MAC, constant-time, over the canonical body;
      * when `report` is supplied, that the report in hand hashes to the value
        the receipt was signed over. A report edited after issue is a report
        whose receipt no longer vouches for it, which is the whole reason the
        hash is in the signed body.
    """
    refusal = _envelope(receipt)
    if refusal:
        return False, refusal
    if not isinstance(key, (bytes, bytearray)) or not key:
        return False, "no key to verify against"
    try:
        expected = _mac(receipt, bytes(key))
    except attest.NotCanonicalizable as error:
        return False, f"refused: {error}"
    if not hmac.compare_digest(str(receipt.get(SIGNATURE_FIELD)), expected):
        found = str(receipt.get("key_id") or "(no key id)")
        mine = key_id(bytes(key))
        if found != mine:
            return False, (f"signature refused: the receipt was signed by key "
                           f"{found}, this verifier holds {mine}")
        return False, ("signature refused: the body does not match the "
                       "signature (the receipt was altered after it was "
                       "issued)")
    if report is not None:
        live = report_hash(report)
        bound = str((receipt.get("report") or {}).get("hash"))
        if not hmac.compare_digest(live, bound):
            return False, (
                "report binding refused: the report in hand hashes to "
                f"{live[:12]}..., the receipt names {bound[:12]}...")
    return True, ""


# ----------------------------------------------------------------- rendering

def render_receipt(receipt: Mapping) -> str:
    """Human rendering. The structured receipt is the product; this is the
    auditor's readable view. It states scope first and prints each replica with
    its disposition, because a row a reader cannot check is a row a reader
    cannot use."""
    if not isinstance(receipt, Mapping) or receipt.get("kind") != RECEIPT_KIND:
        return "error: not a revl.erasure-receipt document"
    scope = receipt.get("scope") or {}
    out = [
        f"ERASURE RECEIPT: realm `{receipt.get('realm')}`",
        f"  {receipt.get('kind')} v{receipt.get('version')}",
        f"  issued {receipt.get('issued_at')}"
        + (f" by {receipt['signer']}" if receipt.get("signer") else ""),
        f"  key {receipt.get('key_id')}",
        f"  report {str((receipt.get('report') or {}).get('hash'))[:16]}... "
        f"({(receipt.get('report') or {}).get('kind')} v"
        f"{(receipt.get('report') or {}).get('schema_version')})",
        "",
        f"  {scope.get('title', '')}",
        "  PROVES:",
    ]
    out += [f"    + {line}" for line in scope.get("proves") or []]
    out.append("  DOES NOT PROVE:")
    out += [f"    - {line}" for line in scope.get("doesNotProve") or []]
    out.append(f"    ({scope.get('reference', '')})")
    state = receipt.get("inProcess") or {}
    # an auditor reading the rendered view must see WHY it is not `reclaimed`:
    # the checks that did not hold, or the reason no proof was taken. Both are
    # signed members of the row, so this is the row speaking, not the renderer.
    note = ""
    if state.get("disposition") != "reclaimed":
        failed = state.get("failedChecks") or []
        if failed:
            note = f"; checks failed: {', '.join(failed)}"
        elif state.get("reason"):
            note = f"; not measured: {state['reason']}"
    out += [
        "",
        f"  [in-process] {state.get('replica')}: "
        f"{state.get('disposition')}"
        f" ({len(state.get('provisionsErased') or [])} provision(s) erased"
        f"{note})",
    ]
    rows = receipt.get("replicas") or []
    out.append("")
    if not rows:
        out.append("  [replicas] none: this realm crosses no boundary the "
                   "compiler can see")
    else:
        out.append(f"  [replicas] {len(rows)} boundary crossing(s)")
        for row in rows:
            inverse = f" via {row['inverse']}" if row.get("inverse") else ""
            caps = f" [{', '.join(row.get('capabilities') or [])}]" \
                if row.get("capabilities") else ""
            out.append(f"    {row.get('disposition'):<11} {row.get('replica')}"
                       f"{caps}{inverse}")
    by = (receipt.get("summary") or {}).get("byDisposition") or {}
    out.append("")
    out.append("  summary: " + ", ".join(
        f"{name} {by.get(name, 0)}" for name in ALL_DISPOSITIONS if by.get(name)))
    out.append(f"  signature: {receipt.get(SIGNATURE_FIELD)}")
    return "\n".join(out)

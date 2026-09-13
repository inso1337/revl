"""Typed data retention — `Retained[T, <policy>]` and the erasure receipt it
authorises (roadmap item 472, issue #824).

`docs/design/472-retention-erasure-receipts.md` landed the erasure half
(`erasure_receipt`) and named four prerequisites for the retention half: a sink
the type system owns, a time fact the checker can compare, the residence and
legal-hold vocabulary, and a derivative relation. This module supplies the
vocabulary and the two of those four that are honestly reachable, and it says
which parts it does NOT supply rather than implying the whole item is done.

WHAT IS ENFORCED HERE

  * `retention <name> { ... }` (parser.RetentionDecl) declares the five facts
    the item asks for: a retention deadline (`until`), a geographic residence
    (`residence`), a legal-hold exception (`hold`), who may request deletion
    (`deleters`), and whether derivatives are in scope (`derivatives`).
  * `Retained[T, <policy>]` is a QUALIFIER in `taint.py`'s family, beside
    `Untrusted[T]` / `Trusted[T]` / `Secret[T]`. Like them it is stripped to its
    base type before the base checker or any emitter sees it, so the IR and
    every backend are byte-identical for a program that uses no qualifier, and
    no backend change is needed for one that does.
  * The PERSISTENCE SINKS are `PERSISTENCE_SINK_SCOPES`, derived from a
    crossing's declared capability scope exactly the way `taint._SINK_CLASS_SCOPES`
    derives an authority sink. A `Retained[T, P]` value reaching one of those
    crossings when `P`'s deadline has passed, and no legal hold is declared, is
    refused at admission with `G-RETAIN` naming the sink.
  * A declared legal hold OVERRIDES the deadline, because that is what a legal
    hold is: an instruction to keep data past the date it would otherwise be
    disposed of. The override is a declaration, so it is visible in the source,
    in the refusal's absence, and in every receipt issued under the policy.

WHERE THE TIME FACT COMES FROM, PLAINLY

The checker has no clock of its own. The deadline is compared against ONE
evaluation instant per compile, `evaluation_instant()`: `REVL_RETENTION_AS_OF`
when set (an RFC-3339 instant, which is what makes a build reproducible and a
test deterministic), otherwise the compile's own wall clock. That makes the
refusal a property of ADMISSION rather than of the source text: the same program
is admitted before the deadline and refused after it, which is the only reading
of "past its retention deadline" that a static checker can hold. It is not a
runtime age fact about an individual record — prerequisite 2 of the design note
is supplied at the granularity of the DECLARED policy, not per value, and this
module does not pretend otherwise.

WHAT A RECEIPT IS, AND WHAT IT IS NOT

`make_receipt` signs an ENUMERATION, not a proof of destruction. revl cannot
observe a remote replica's disks. What the signature establishes is: this is the
set of replicas and derivatives the system knows about, this is the policy they
were held under, this is who requested the deletion and whether that requester
was authorised by the policy, and this document has not been altered since. A
copy made outside revl's boundary was never a crossing, so it is never a row,
and no signature can name it. A derivative the declaration does not cover is
listed as `not-covered` and explicitly NOT claimed erased, because the honest
answer to "was the embedding index deleted?" under a policy that never covered
embeddings is "the policy does not reach it", never silence.

WHAT IS STILL NOT IMPLEMENTED (the rest of the design note's prerequisites)

  * The derivative RELATION is declared, not inferred. A policy says which
    CLASSES of derivative it covers; nothing here proves that a particular
    boundary crossing wrote a summary of a particular `Retained` value. Rows
    come from the caller's enumeration (the erase report's crossings, or an
    operator's inventory), and `covered` is read off the declaration.
  * The residence is CHECKED against the replica rows a caller supplies, not
    against a placement fact the compiler derives. A replica whose residence
    differs from the declared one is reported as `residence-mismatch`; nothing
    here can refuse a host from storing bytes in a region it did not declare.
  * There is no per-value creation instant, so nothing here refuses an
    individual record on its own age (see above).

THREE SIGNED PROTOCOLS, ONE CONSTRUCTION. The MAC is `erasure_receipt`'s
construction under this protocol's own domain tag, and the canonical bytes and
key file rule are `attest`'s, CALLED rather than restated, for the reason
`erasure_receipt` gives: a second reading of one rule is how the two drift.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional

from . import attest
from .errors import RevlError

# --------------------------------------------------------------- the qualifier

#: The qualifier head. Registered in `taint._QUALIFIERS` so it is stripped to
#: its base type with the rest of the family (`Retained[Str, pii]` -> `Str`).
QUALIFIER = "Retained"

#: A `Retained[T, P]` value carries this origin in the taint lattice, with the
#: policy name appended. Kept DISJOINT from every member of
#: `taint._ORIGIN_CLASSES` by the `retained:` prefix, so no existing sink,
#: declassifier or audit rule can accidentally read a retention fact as a
#: provenance one, and vice versa.
ORIGIN_PREFIX = "retained:"


def origin_for(policy: str) -> str:
    """The taint origin a `Retained[T, <policy>]` value carries."""
    return f"{ORIGIN_PREFIX}{policy}"


def policy_of_origin(origin) -> Optional[str]:
    """The policy name behind a retention origin, or None for any other origin."""
    if isinstance(origin, str) and origin.startswith(ORIGIN_PREFIX):
        return origin[len(ORIGIN_PREFIX):]
    return None


def is_retention_origin(origin) -> bool:
    return policy_of_origin(origin) is not None


# ----------------------------------------------------------- persistence sinks

#: The capability scope heads that mean DURABLE STORAGE — the persistence sinks,
#: prerequisite 1 of the design note. The sibling of `taint._SINK_CLASS_SCOPES`
#: and derived the same way: sink-ness is read off the capability the crossing
#: DECLARES, never off an author qualifier, because the side that owns the store
#: is the side that knows it is a store.
#:
#: `fs` is deliberately in both sets: in `taint._SOURCE_CLASS_SCOPES` it is a
#: provenance source (bytes read off a disk are untrusted input) and here it is
#: a persistence sink (bytes written to a disk outlive the process). The two
#: dimensions are orthogonal and a filesystem crossing is honestly both.
PERSISTENCE_SINK_SCOPES = frozenset({
    "db",        # a relational or document store
    "fs",        # a filesystem write
    "store",     # a generic durable store
    "kv",        # a key/value store
    "blob",      # object/blob storage
    "archive",   # cold storage
    "index",     # a search index (where a derivative usually lands)
    "cache",     # a durable cache; short-lived, but it is still a copy
    "queue",     # a durable queue retains the payload until consumed
    "wal",       # a write-ahead log
})


def persistence_sink_of(capabilities) -> Optional[str]:
    """The durable-storage scope head a crossing declares, or None when the
    crossing is not a persistence sink. The exact shape of `taint._sink_of`, on
    this module's scope set: only the FIRST capability is consulted for its
    head, because a crossing declares one resource scope."""
    for cap in capabilities or ():
        head = str(cap).split(".", 1)[0]
        if head in PERSISTENCE_SINK_SCOPES:
            return head
    return None


# ------------------------------------------------------------------ the policy

#: The closed derivative vocabulary. A derivative is a value MADE FROM a
#: retained value that outlives it if nobody goes looking: the item names
#: summaries, indexes and embeddings, and the three storage shapes below are the
#: other places a copy hides. Closed on purpose — an open vocabulary would let a
#: policy "cover" a class no erasure path knows how to enumerate, which reads as
#: a guarantee and is not one.
DERIVATIVE_CLASSES = ("summary", "index", "embedding", "backup", "export",
                      "cache")

#: `derivatives: all` covers every class above; omitting the field covers none.
#: Both spellings are explicit in the source, which is the point: "whether
#: derivatives are covered" is a declared fact, never a default nobody wrote.
DERIVATIVES_ALL = "all"
DERIVATIVES_NONE = "none"

_POLICY_FIELDS = ("until", "residence", "hold", "deleters", "derivatives")
_STRING_FIELDS = ("until", "residence", "hold")
_LIST_FIELDS = ("deleters", "derivatives")
_REQUIRED_FIELDS = ("until", "residence", "deleters")

_RESIDENCE_RE = re.compile(r"^[a-z][a-z0-9-]*$")

#: Overridden by `REVL_RETENTION_AS_OF` (an RFC-3339 instant). See the module
#: docstring: one evaluation instant per compile, so a refusal is reproducible.
AS_OF_ENV = "REVL_RETENTION_AS_OF"


def parse_instant(text: str, *, what: str, filename: str, line: int) -> datetime:
    """An RFC-3339 instant with an explicit UTC offset, or a refusal naming the
    field. The offset is REQUIRED: a retention deadline written without one
    means a different moment in every deployment region, and this is a module
    about where and until when bytes may live."""
    raw = str(text).strip()
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise RevlError(
            filename, line,
            f"{what} is not an RFC-3339 instant: {raw!r}",
            hint='write it as `"2027-01-01T00:00:00Z"` — a date with an '
                 "explicit UTC offset, so the deadline names one moment "
                 "rather than one per region") from None
    if value.tzinfo is None:
        raise RevlError(
            filename, line,
            f"{what} has no UTC offset: {raw!r}",
            hint='append `Z` (or an explicit offset such as `+02:00`) — a '
                 "retention deadline without one is a different moment in "
                 "every region the data lives in")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class RetentionPolicy:
    """One validated `retention` declaration: the five facts item 472 asks a
    `Retained[T]` to carry, in the one place they are written."""

    name: str
    #: the retention deadline; past it, a persistence sink refuses the value
    until: datetime
    #: the geographic residence the data is declared to live in
    residence: str
    #: the legal-hold exception, or None. A hold OVERRIDES the deadline.
    hold: Optional[str]
    #: who may request deletion under this policy
    deleters: tuple
    #: the derivative classes this policy covers (a subset of
    #: `DERIVATIVE_CLASSES`); empty means the policy covers no derivative
    derivatives: tuple
    line: int = 0

    @property
    def held(self) -> bool:
        """Whether a legal hold is in force. A held policy never expires at a
        persistence sink: keeping the data is the hold's whole instruction."""
        return bool(self.hold)

    def expired(self, as_of: datetime) -> bool:
        """Whether the deadline has passed at `as_of`, hold taken into account.

        The legal hold is applied HERE rather than at the call sites, so no
        caller can consult the deadline while forgetting the exception."""
        if self.held:
            return False
        return as_of > self.until

    def covers_derivative(self, kind: str) -> bool:
        return kind in self.derivatives

    def may_delete(self, requester: str) -> bool:
        return requester in self.deleters

    def as_json(self) -> dict:
        """The policy as it appears INSIDE a signed receipt body."""
        return {
            "name": self.name,
            "until": self.until.isoformat().replace("+00:00", "Z"),
            "residence": self.residence,
            "hold": self.hold,
            "deleters": list(self.deleters),
            "derivatives": list(self.derivatives),
        }


def _field_map(decl, filename: str) -> dict:
    """`decl.fields` folded to `{key: (values, was_string, line)}`, refusing an
    unknown key and a duplicate one by name."""
    seen: dict = {}
    for key, values, was_string, line in decl.fields:
        if key not in _POLICY_FIELDS:
            known = ", ".join(f"`{k}`" for k in _POLICY_FIELDS)
            raise RevlError(
                filename, line,
                f"`retention {decl.name}` has no field `{key}`",
                hint=f"a retention policy declares {known} (item 472)")
        if key in seen:
            raise RevlError(
                filename, line,
                f"`retention {decl.name}` declares `{key}` twice",
                hint="one policy states each fact once; two spellings of a "
                     "deadline is two policies")
        seen[key] = (values, was_string, line)
    return seen


def policy_from_decl(decl, filename: str) -> RetentionPolicy:
    """Validate one `retention` declaration into a `RetentionPolicy`.

    Every rule the parser deliberately did not enforce is enforced here, so the
    rule and the refusal that carries it sit in one file: the key set, the
    string-vs-list shape of each field, the RFC-3339 deadline, the residence
    token, the non-empty deleter set and the closed derivative vocabulary."""
    fields = _field_map(decl, filename)
    for required in _REQUIRED_FIELDS:
        if required not in fields:
            raise RevlError(
                filename, decl.line,
                f"`retention {decl.name}` is missing `{required}`",
                hint="a policy that does not say when data must go, where it "
                     "lives, or who may ask for it to go is not a retention "
                     "policy — `until`, `residence` and `deleters` are "
                     "required (item 472)")
    for key in _STRING_FIELDS:
        if key in fields and not fields[key][1]:
            raise RevlError(
                filename, fields[key][2],
                f"`{key}` must be a string in `retention {decl.name}`",
                hint=f'write `{key}: "..."`')
    for key in _LIST_FIELDS:
        if key in fields and fields[key][1]:
            raise RevlError(
                filename, fields[key][2],
                f"`{key}` must be a list of names in `retention {decl.name}`",
                hint=f"write `{key}: one, two` — names, not a string")

    until = parse_instant(fields["until"][0][0], what=f"`until` in "
                          f"`retention {decl.name}`", filename=filename,
                          line=fields["until"][2])
    residence = fields["residence"][0][0]
    if not _RESIDENCE_RE.match(residence):
        raise RevlError(
            filename, fields["residence"][2],
            f"`residence` is not a region token: {residence!r}",
            hint='a residence is a lowercase region name such as `"eu"`, '
                 '`"de"` or `"us-east-1"`')
    hold = fields["hold"][0][0] if "hold" in fields else None
    if hold is not None and not hold.strip():
        raise RevlError(
            filename, fields["hold"][2],
            f"`hold` in `retention {decl.name}` is empty",
            hint="a legal hold overrides the deadline, so it has to NAME the "
                 "hold (a matter number, an order reference) — omit the field "
                 "when no hold is in force")

    deleters = fields["deleters"][0]
    if not deleters:
        raise RevlError(
            filename, fields["deleters"][2],
            f"`deleters` in `retention {decl.name}` is empty",
            hint="name at least one principal that may request deletion; a "
                 "policy nobody can act on is not a deletion right")
    if len(set(deleters)) != len(deleters):
        raise RevlError(
            filename, fields["deleters"][2],
            f"`deleters` in `retention {decl.name}` names a principal twice",
            hint="each principal appears once")

    declared = fields["derivatives"][0] if "derivatives" in fields else ()
    derivatives = _derivatives_from(declared, decl, filename,
                                    fields.get("derivatives", ((), False, decl.line))[2])
    return RetentionPolicy(decl.name, until, residence, hold, tuple(deleters),
                           derivatives, decl.line)


def _derivatives_from(declared, decl, filename: str, line: int) -> tuple:
    """The covered derivative classes, from the declared list.

    `all` covers every class and `none` covers nothing; anything else is a
    subset of `DERIVATIVE_CLASSES`. The two sentinels may not be mixed with
    names, because `summary, none` says two opposite things."""
    if not declared:
        return ()
    if DERIVATIVES_ALL in declared or DERIVATIVES_NONE in declared:
        if len(declared) != 1:
            raise RevlError(
                filename, line,
                f"`derivatives` in `retention {decl.name}` mixes "
                f"`{DERIVATIVES_ALL}`/`{DERIVATIVES_NONE}` with named classes",
                hint="write either the sentinel alone or the list of classes")
        return DERIVATIVE_CLASSES if declared[0] == DERIVATIVES_ALL else ()
    unknown = [d for d in declared if d not in DERIVATIVE_CLASSES]
    if unknown:
        known = ", ".join(f"`{k}`" for k in DERIVATIVE_CLASSES)
        raise RevlError(
            filename, line,
            f"`derivatives` in `retention {decl.name}` names unknown "
            f"derivative class{'es' if len(unknown) > 1 else ''} "
            + ", ".join(f"`{u}`" for u in unknown),
            hint=f"the vocabulary is closed: {known}, or `all`/`none`. A class "
                 "no erasure path can enumerate would read as a guarantee "
                 "without being one (item 472)")
    if len(set(declared)) != len(declared):
        raise RevlError(
            filename, line,
            f"`derivatives` in `retention {decl.name}` names a class twice",
            hint="each class appears once")
    return tuple(d for d in DERIVATIVE_CLASSES if d in declared)


def policies(program, filename: Optional[str] = None) -> dict:
    """Every validated policy a program declares, keyed by name.

    Empty (and free) for a program that declares none, which is what keeps the
    whole retention surface inert for every program that does not use it."""
    decls = getattr(program, "retentions", None) or ()
    if not decls:
        return {}
    where = filename or getattr(program, "filename", "<retention>")
    out: dict = {}
    for decl in decls:
        if decl.name in out:
            raise RevlError(
                where, decl.line,
                f"duplicate `retention {decl.name}`",
                hint="one policy per name; a second declaration would make "
                     "`Retained[T, " + decl.name + "]` ambiguous")
        out[decl.name] = policy_from_decl(decl, where)
    return out


def evaluation_instant(env=None) -> datetime:
    """The one instant a compile compares every retention deadline against.

    `REVL_RETENTION_AS_OF` wins when set, so a build is reproducible and a test
    is deterministic; otherwise it is the compile's own UTC wall clock. See the
    module docstring: this makes the retention refusal a property of ADMISSION,
    which is the only reading of "past its deadline" a static checker can hold."""
    env = os.environ if env is None else env
    raw = env.get(AS_OF_ENV)
    if not raw:
        return datetime.now(timezone.utc)
    return parse_instant(raw, what=f"`{AS_OF_ENV}`", filename=f"<{AS_OF_ENV}>",
                         line=0)


# ------------------------------------------------------------- the receipt

RECEIPT_VERSION = "1.0"
RECEIPT_KIND = "revl.retention-erasure-receipt"

#: This protocol's own domain tag. `attest` (item 127), `deploy` (item 478),
#: `erasure_receipt` (item 472's first half) and this all MAC the same shape of
#: document with the same construction, so without a distinct tag a retention
#: receipt would verify as one of the others under the same key.
RECEIPT_DOMAIN = b"revl.retention-erasure-receipt/v1\x00"
SIGNATURE_FIELD = "signature"
HASH_ALG = "sha256"
SIGN_ALG = "hmac-sha256"

KEY_ENV = "REVL_ERASURE_KEY"
KEY_FILE_ENV = "REVL_ERASURE_KEY_FILE"
KEY_ID_DOMAIN = b"revl-retention-keyid\x00"

#: What a REPLICA row can say. `enumerated` is the plain reading: the system
#: knows about this copy and the request covers it. `withheld:legal-hold` is a
#: copy the policy forbids erasing right now. `residence-mismatch` is a copy
#: sitting outside the residence the policy declared, which is a finding an
#: auditor needs and never something to quietly erase-and-forget.
REPLICA_DISPOSITIONS = ("enumerated", "withheld:legal-hold",
                        "residence-mismatch")

#: What a DERIVATIVE row can say. `covered` means the policy names this class;
#: `not-covered` means it does not, and the row exists precisely so the reader
#: is told rather than left to assume. `withheld:legal-hold` mirrors the replica
#: reading. Kept APART from `REPLICA_DISPOSITIONS` so an envelope check for one
#: row shape cannot be satisfied by the other's vocabulary.
DERIVATIVE_DISPOSITIONS = ("covered", "not-covered", "withheld:legal-hold")

#: The one sentence a `not-covered` row carries, inside the signed body. A
#: reader who holds only the receipt must be able to tell that this derivative
#: was NOT claimed erased without knowing this module's vocabulary.
NOT_CLAIMED = "not claimed erased: the policy does not cover this derivative class"

SCOPE = {
    "title": "What this receipt proves, and what it does not",
    "proves": [
        "the enumeration is unaltered: an HMAC-SHA256 over the canonical body "
        "under this protocol's own domain tag, so a row added, dropped or "
        "edited after issue stops verifying.",
        "the request was authorised by the policy: the requester is a member "
        "of the policy's own `deleters`, and the policy is inside the signed "
        "body rather than beside it.",
        "every replica and derivative the system knows about is NAMED, with "
        "the disposition the policy assigns it — including each derivative "
        "the policy does NOT cover, which is listed as `not-covered` and is "
        "explicitly not claimed erased.",
    ],
    "doesNotProve": [
        "that any byte was destroyed. This is an ENUMERATION plus a "
        "signature, not a proof of destruction: revl cannot observe a remote "
        "replica's disks, and nothing here asks a remote host whether it "
        "complied.",
        "that the enumeration is complete. A copy made outside revl's "
        "boundary was never a crossing, so it is never a row, and no "
        "signature can name it.",
        "that a covered derivative was reached. `covered` is read off the "
        "declaration; the derivative relation is declared, not inferred.",
        "that a replica actually lives in the declared residence. A row whose "
        "residence differs is reported as `residence-mismatch`; a row that "
        "agrees is reporting what the caller supplied.",
    ],
}


@dataclass(frozen=True)
class Replica:
    """One copy of a retained value the system knows about.

    `token` is the caller's identity for it — for a composition that is an erase
    report crossing token (`host:<component>:<extern>`), for an operator
    inventory it is whatever names the store. `residence` is where the caller
    says the copy lives, checked against the policy's declared residence."""

    token: str
    residence: Optional[str] = None
    kind: str = "boundary"


@dataclass(frozen=True)
class Derivative:
    """One value MADE FROM a retained value: a summary, a search index entry, an
    embedding, a backup, an export, a cache entry.

    `kind` is a `DERIVATIVE_CLASSES` member; a kind outside the vocabulary is
    refused rather than written into a receipt as if the policy could cover it."""

    name: str
    kind: str
    token: Optional[str] = None
    residence: Optional[str] = None


def load_key(path) -> bytes:
    """`attest.load_key`'s rule, delegated to rather than restated (the reason
    is `erasure_receipt.load_key`'s: two implementations of a key file made the
    same `cat`-created file two different keys)."""
    return attest.load_key(str(path))


def key_id(key: bytes) -> str:
    """A non-secret, domain-tagged fingerprint of the signing key, so a verifier
    can say WHICH key it needs. Tagged with this protocol's own domain, so a
    retention key fingerprint and an erasure-report key fingerprint of the same
    bytes are different strings rather than accidentally equal."""
    return hashlib.sha256(KEY_ID_DOMAIN + bytes(key)).hexdigest()[:16]


def _replica_row(replica: Replica, policy: RetentionPolicy) -> dict:
    if policy.held:
        disposition = "withheld:legal-hold"
    elif replica.residence is not None and replica.residence != policy.residence:
        disposition = "residence-mismatch"
    else:
        disposition = "enumerated"
    return {
        "token": replica.token,
        "kind": replica.kind,
        "residence": replica.residence,
        "disposition": disposition,
    }


def _derivative_row(derivative: Derivative, policy: RetentionPolicy) -> dict:
    covered = policy.covers_derivative(derivative.kind)
    if policy.held and covered:
        disposition = "withheld:legal-hold"
    else:
        disposition = "covered" if covered else "not-covered"
    row = {
        "name": derivative.name,
        "derivativeKind": derivative.kind,
        "token": derivative.token,
        "residence": derivative.residence,
        "covered": covered,
        "disposition": disposition,
    }
    if not covered:
        # The claim is a member of the row rather than a note beside it, so a
        # reader who holds only the receipt cannot mistake silence for erasure.
        row["claim"] = NOT_CLAIMED
    return row


def _tally(rows, key: str, vocabulary) -> dict:
    counts = {name: 0 for name in vocabulary}
    for row in rows:
        counts[row[key]] = counts.get(row[key], 0) + 1
    return counts


def build_body(policy: RetentionPolicy, requester: str, replicas, derivatives,
               *, now=None, signer: Optional[str] = None,
               key: Optional[bytes] = None) -> dict:
    """The unsigned receipt body: every member except the signature.

    `make_receipt` signs exactly this and `verify_receipt` re-MACs exactly the
    members it receives, so any altered, dropped or added member breaks the
    signature — the whole record is committed, never a chosen subset."""
    replica_rows = [_replica_row(r, policy) for r in replicas]
    derivative_rows = [_derivative_row(d, policy) for d in derivatives]
    issued = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    uncovered = sorted(r["name"] for r in derivative_rows if not r["covered"])
    return {
        "kind": RECEIPT_KIND,
        "version": RECEIPT_VERSION,
        "hashAlg": HASH_ALG,
        "signAlg": SIGN_ALG,
        "issuedAt": issued.isoformat().replace("+00:00", "Z"),
        **({"signer": signer} if signer else {}),
        **({"key_id": key_id(key)} if key else {}),
        "policy": policy.as_json(),
        "request": {"requester": requester, "authorisedBy": "policy.deleters"},
        "replicas": replica_rows,
        "derivatives": derivative_rows,
        "summary": {
            "replicas": len(replica_rows),
            "derivatives": len(derivative_rows),
            "coveredDerivatives": sum(1 for r in derivative_rows if r["covered"]),
            "uncoveredDerivatives": uncovered,
            "byReplicaDisposition": _tally(replica_rows, "disposition",
                                           REPLICA_DISPOSITIONS),
            "byDerivativeDisposition": _tally(derivative_rows, "disposition",
                                              DERIVATIVE_DISPOSITIONS),
            "legalHold": policy.hold,
        },
        "scope": SCOPE,
    }


def _mac(body: Mapping, key: bytes) -> str:
    """Domain-tagged HMAC-SHA256 over `attest._canonical_bytes(body)`, the
    construction `attest._sign`, `deploy._receipt_mac` and
    `erasure_receipt._mac` share. Deferred to rather than restated so a
    third-party verifier following docs/revl-attest.md recomputes these exact
    bytes — which is only true while there is one implementation of the
    spelling."""
    return hmac.new(bytes(key), RECEIPT_DOMAIN + attest._canonical_bytes(
        {k: v for k, v in body.items() if k != SIGNATURE_FIELD}),
        hashlib.sha256).hexdigest()


def make_receipt(policy: RetentionPolicy, requester: str, replicas,
                 derivatives, key: bytes, *, now=None,
                 signer: Optional[str] = None) -> dict:
    """Sign one erasure request under one policy.

    Pure and deterministic given `now`: the same inputs always produce
    byte-identical output, which is what makes the round trip testable and the
    receipt reproducible from the evidence it summarises.

    THREE refusals, and each of them is a refusal rather than a footnote in a
    signed document:

      * an UNAUTHORISED requester. "Who may request deletion" is one of the
        five facts the policy carries, so a request from anyone else is not a
        request this policy can answer, and signing it would put the policy's
        authority behind an act the policy does not grant.
      * a derivative whose `kind` is outside `DERIVATIVE_CLASSES`. A policy can
        only cover classes it can name, so an unknown kind would be written as
        `not-covered` and read as "the policy considered it", which it did not.
      * a body with no canonical byte spelling (a `signer` that is not UTF-8
        text), raised as a `RevlError` naming the argument rather than escaping
        as `attest.NotCanonicalizable`. There is no partially-signed receipt."""
    if not isinstance(key, (bytes, bytearray)) or not key:
        raise RevlError("<retention-receipt>", 0,
                        "the signing key must be non-empty bytes")
    if not policy.may_delete(requester):
        allowed = ", ".join(f"`{d}`" for d in policy.deleters)
        raise RevlError(
            "<retention-receipt>", policy.line,
            f"`{requester}` may not request deletion under `retention "
            f"{policy.name}`",
            hint=f"the policy names {allowed}. Who may request deletion is one "
                 "of the facts the policy carries, so a request from anyone "
                 "else is not one this policy can answer (item 472)")
    for derivative in derivatives:
        if derivative.kind not in DERIVATIVE_CLASSES:
            known = ", ".join(f"`{k}`" for k in DERIVATIVE_CLASSES)
            raise RevlError(
                "<retention-receipt>", policy.line,
                f"derivative `{derivative.name}` has unknown class "
                f"`{derivative.kind}`",
                hint=f"the vocabulary is closed: {known}. An unknown class "
                     "would be written `not-covered`, which reads as \"the "
                     "policy considered it\" and it did not")
    try:
        body = build_body(policy, requester, replicas, derivatives, now=now,
                          signer=signer, key=bytes(key))
        signature = _mac(body, bytes(key))
    except attest.NotCanonicalizable as error:
        raise RevlError(
            "<retention-receipt>", 0,
            "the receipt cannot be signed: this document has no canonical byte "
            f"spelling ({error}), so no conforming verifier could recompute a "
            "signature over it. A `signer` name that is not UTF-8 text is the "
            "usual cause; drop it to omit the name") from error
    return {**body, SIGNATURE_FIELD: signature}


def _envelope(receipt: Mapping) -> str:
    """The envelope check: is this record even a retention erasure receipt of
    the shape this verifier accepts? Returns a refusal reason, or `""`.

    A MAC proves authorship, not that what was authored means what the reader
    assumes. Every member whose value carries a fixed meaning is checked against
    another member of the SAME signed body — nothing here re-derives anything
    from unsigned input:

      * the labels (`kind`, `version`, the two algorithms), so an `attest`
        verdict or an `erasure_receipt` presented as this is a mislabel;
      * each row's disposition against its own vocabulary, kept apart for the
        two row shapes;
      * each derivative row's `covered` against the POLICY's own `derivatives`
        member, so a receipt cannot claim a derivative covered by a policy that
        does not cover it (the defect that would let a `not-covered` row be
        re-read as erased);
      * an uncovered row carries the `not claimed erased` sentence and no
        covered row carries it, so the claim cannot be detached from the row;
      * the requester against the policy's own `deleters`;
      * every tally against the rows it counts, so the line a reader quotes
        cannot disagree with the rows printed beside it.
    """
    def reason(member, expected, found):
        return (f"envelope refused: {member} is {found!r}, expected "
                f"{expected!r}")

    if not isinstance(receipt, Mapping):
        return "envelope refused: the receipt is not a document"
    if receipt.get("kind") != RECEIPT_KIND:
        return reason("kind", RECEIPT_KIND, receipt.get("kind"))
    if receipt.get("version") != RECEIPT_VERSION:
        return reason("version", RECEIPT_VERSION, receipt.get("version"))
    if receipt.get("hashAlg") != HASH_ALG:
        return reason("hashAlg", HASH_ALG, receipt.get("hashAlg"))
    if receipt.get("signAlg") != SIGN_ALG:
        return reason("signAlg", SIGN_ALG, receipt.get("signAlg"))
    if not receipt.get(SIGNATURE_FIELD):
        return "envelope refused: the receipt carries no signature"

    policy = receipt.get("policy")
    if not isinstance(policy, Mapping):
        return reason("policy", "the policy the rows were held under", policy)
    covered_classes = policy.get("derivatives")
    if not isinstance(covered_classes, list):
        return reason("policy.derivatives", "a list of derivative classes",
                      covered_classes)
    deleters = policy.get("deleters")
    if not isinstance(deleters, list):
        return reason("policy.deleters", "a list of principals", deleters)
    held = bool(policy.get("hold"))

    request = receipt.get("request")
    if not isinstance(request, Mapping):
        return reason("request", "the erasure request", request)
    if request.get("requester") not in deleters:
        return ("envelope refused: request.requester "
                f"{request.get('requester')!r} is not one of the policy's own "
                f"deleters {deleters!r}")

    replicas = receipt.get("replicas")
    if not isinstance(replicas, list):
        return reason("replicas", "a list of replica rows", replicas)
    for row in replicas:
        if not isinstance(row, Mapping):
            return reason("replicas[]", "a replica row", row)
        if row.get("disposition") not in REPLICA_DISPOSITIONS:
            return reason("replicas[].disposition",
                          f"one of {list(REPLICA_DISPOSITIONS)}",
                          row.get("disposition"))
        if held and row["disposition"] != "withheld:legal-hold":
            return ("envelope refused: a legal hold is declared "
                    f"({policy.get('hold')!r}) while replica "
                    f"{row.get('token')!r} is {row['disposition']!r} — under a "
                    "hold no replica may be reported as erasable")

    derivatives = receipt.get("derivatives")
    if not isinstance(derivatives, list):
        return reason("derivatives", "a list of derivative rows", derivatives)
    for row in derivatives:
        if not isinstance(row, Mapping):
            return reason("derivatives[]", "a derivative row", row)
        if row.get("disposition") not in DERIVATIVE_DISPOSITIONS:
            return reason("derivatives[].disposition",
                          f"one of {list(DERIVATIVE_DISPOSITIONS)}",
                          row.get("disposition"))
        covered = row.get("covered")
        if not isinstance(covered, bool):
            return reason("derivatives[].covered", "a bool", covered)
        # The load-bearing check: `covered` is not an independent claim, it is a
        # reading of the policy that travels in the same signed body. Without
        # this, a receipt could name an embedding index `covered` under a policy
        # that covers only summaries, which is exactly the silent over-claim
        # this row shape exists to prevent.
        if covered != (row.get("derivativeKind") in covered_classes):
            return ("envelope refused: derivative "
                    f"{row.get('name')!r} of class "
                    f"{row.get('derivativeKind')!r} is marked covered="
                    f"{covered!r} while the policy covers {covered_classes!r}")
        if not covered:
            if row.get("claim") != NOT_CLAIMED:
                return ("envelope refused: uncovered derivative "
                        f"{row.get('name')!r} does not carry the "
                        "`not claimed erased` statement")
            if row["disposition"] != "not-covered":
                return reason("derivatives[].disposition", "not-covered",
                              row["disposition"])
        else:
            if "claim" in row:
                return ("envelope refused: covered derivative "
                        f"{row.get('name')!r} carries a `not claimed erased` "
                        "statement")
            expected = "withheld:legal-hold" if held else "covered"
            if row["disposition"] != expected:
                return reason("derivatives[].disposition", expected,
                              row["disposition"])

    summary = receipt.get("summary")
    if not isinstance(summary, Mapping):
        return reason("summary", "the receipt's own tally", summary)
    checks = (
        ("replicas", len(replicas)),
        ("derivatives", len(derivatives)),
        ("coveredDerivatives", sum(1 for r in derivatives if r["covered"])),
        ("uncoveredDerivatives",
         sorted(r["name"] for r in derivatives if not r["covered"])),
        ("byReplicaDisposition",
         _tally(replicas, "disposition", REPLICA_DISPOSITIONS)),
        ("byDerivativeDisposition",
         _tally(derivatives, "disposition", DERIVATIVE_DISPOSITIONS)),
    )
    for member, want in checks:
        got = summary.get(member)
        if isinstance(want, dict):
            got = dict(got) if isinstance(got, Mapping) else got
        if got != want:
            return ("envelope refused: summary." + member + " is not the tally "
                    f"of this receipt's own rows: it says {got!r}, the rows "
                    f"count {want!r}")
    if summary.get("legalHold") != policy.get("hold"):
        return reason("summary.legalHold", policy.get("hold"),
                      summary.get("legalHold"))
    return ""


def verify_receipt(receipt: Mapping, key: bytes) -> tuple:
    """Verify a retention erasure receipt. Returns `(ok, reason)`; `reason` is
    `""` when `ok`.

    The envelope first (the cheapest refusal wins, and a MAC on a mislabelled
    document is still a mislabelled document), then the MAC in constant time."""
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
    return True, ""


def uncovered_derivatives(receipt: Mapping) -> list:
    """The derivatives a receipt reports as NOT covered by its policy.

    The one-line answer to "what did this erasure not reach", read off the
    signed body. A caller that prints a receipt should print this too: a
    derivative the policy never covered is the copy most likely to be assumed
    gone."""
    return [
        {"name": row.get("name"), "derivativeKind": row.get("derivativeKind"),
         "token": row.get("token"), "claim": row.get("claim")}
        for row in (receipt.get("derivatives") or ())
        if isinstance(row, Mapping) and row.get("covered") is False
    ]

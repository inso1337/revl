"""Multi-party approval, Slice 2: the operator verbs and the admission receipt.

Roadmap item 471 (issue #823), design `docs/design/471-quorum-approval.md`.
Slice 1 landed the policy clause (`require N of {a, b, c}`), the session-side
vote protocol, the nine `quorum-*` WAL record kinds that make the decision graph
durable, and separation of duties. It left two things design-only, and this
module is both of them:

  * **the operator verbs.** `Session.escalate_ticket`, `revoke_ticket` and
    `override_ticket` existed with no tool and no verb behind them, so an
    operator on the wire could open a question and vote on it but could never
    hand it up, withdraw it, or break the glass. The three adapters here
    (:func:`escalate`, :func:`revoke_question`, :func:`override`) are what
    `revl_escalate` / `revl_revoke` / `revl_override` call, and
    :func:`decision_report` is what the read-only `revl_quorum` calls. They
    validate the transport's argument shape and delegate the protocol unchanged:
    every refusal below is still the session's, recorded in the decision graph
    before it is raised.
  * **the admission receipt.** The item asks for the full decision graph "in the
    WAL and receipt". Slice 1 put it in the WAL. This module adds the receipt,
    and the receipt is a JOIN over those durable rows rather than a tenth row of
    its own.

## Why the receipt is derived and not a new WAL record

A second durable copy of facts the WAL already holds is a record that can
disagree with itself, and an audit that finds two answers has no answer. The
decision graph is already append-only and complete: `quorum-open` names the
question and its binding, `quorum-vote` each counted vote, `quorum-refused`
each cast that was not counted and why, and one of `quorum-satisfied` /
`-denied` / `-expired` / `-escalated` / `-revoked` / `-override` closes it. What
was missing was not the facts but the ARTIFACT: one hash-bound document, minted
at the moment the authority is spent, that carries the whole graph for the one
crossing it authorized.

So :func:`build_receipt` assembles it from the graph and the ledger entry, and
:func:`verify_receipt` RE-DERIVES it from those same rows and compares. The rows
it reads are `Session._approval_records`, which `_record_quorum` writes in
lockstep with the WAL and under the same keys, so "re-derived from the graph" and
"re-derived from the durable rows" name one set of facts.

A receipt therefore cannot drift from the record it summarizes. An edited field,
a receipt re-pointed at another candidate hash, a receipt whose votes were
padded, and a receipt for a question the graph does not carry are each refused by
the verifier rather than believed. The digest is over the binding AND the
decision, so it is not a checksum of a summary — it is the identity of one
decision about one candidate.

**Admission time, not decision time.** The receipt is minted where the authority
is SPENT (`Session._consume_approval` / `_commit_spends`, consume-before-fire),
not where the votes closed the question. That is the moment the item's "auditable
after the fact" is about: a satisfied quorum that no crossing ever spent
authorized nothing, and its receipt would name a fire that never happened.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from .approval import _canon, _sha

#: The receipt's own kind tag, so a receipt is legible as a receipt and can
#: never be read as one of the `revl.deploy` / attestation documents (which carry
#: their own domain-separated kinds, `src/revl/deploy.py`).
RECEIPT_KIND = "revl.quorum.receipt"

#: The receipt shape's version. Bumped when a field is added, so a verifier
#: reading an older receipt refuses it rather than re-deriving a digest over a
#: shape it does not know.
RECEIPT_VERSION = 1


# --------------------------------------------------------------------------- #
# Whose cast is it: binding the identity of a vote (issue #979)                #
# --------------------------------------------------------------------------- #
#
# Slice 1 and Slice 2 both took the cast's identity from `as_token`, a STRING
# the caller chose. The count was of distinct NAMES, so one operator satisfied
# `require 2 of {alice, bob, carol}` by asserting two of the names in turn:
# distinctness of names is not distinctness of principals, and multi-party
# control is about the second.
#
# What identity is actually available here decides what can be bound. Two
# things, and only two:
#
#   * the SESSION's own operator, bound once at serve time from
#     `--operator-profile`/`--operator`. Not caller-asserted — it is process
#     configuration the caller on the wire cannot choose — but there is exactly
#     one of it per session, so on its own it can supply one cast and never N.
#   * a VOTE CREDENTIAL the operator profile declares for an operator, issued
#     out of band and presented with the cast. The profile stores the SHA-256
#     digest, so the file is not a list of secrets; a cast naming an operator
#     other than the session's own must present a credential that hashes to
#     that operator's digest.
#
# Everything else at this boundary is a string the caller typed. So the rule is:
# a cast is attributed to the session's bound operator, or to an operator whose
# declared credential the caller proved, and to nothing else. An identity that
# is ambiguous or unbindable REFUSES — there is no path here that admits on a
# name alone.
#
# The PRINCIPAL is the distinctness unit and is derived, not asserted: the
# session binding is one principal, and each distinct credential digest is one
# principal. Two profile entries that share one secret are therefore ONE
# principal and can supply only one of the N (`same-principal`), which a count
# of names could never see.
#
# What this proves, stated narrowly: N counted casts required N distinct
# secrets, or N-1 distinct secrets plus the session's serve-time identity. What
# it does NOT prove is that N humans consented — a credential is bearer, it can
# be shared, delegated or stolen, and every cast still arrives over one
# session's wire, so an operator who has collected two secrets still satisfies a
# two-of-M rule. Closing that needs a per-caller authenticated transport (items
# 39 / 55) where each cast arrives on its own authenticated connection and is
# signed over the question's binding, so a captured credential is not replayable
# and the count is of connections rather than of strings. See
# `docs/design/471-quorum-approval.md`, Decision 7.

#
# ## What a SIGNED cast adds, and what it still does not
#
# The paragraph above names the bearer credential's three bounds, and a signed
# cast (`operator <token> sign p256:<hex>`) answers two of them squarely:
#
#   * a bearer secret is HANDED OVER to cast it. Everything on the path — the
#     session process, the transport, a log, a co-located reader, the proposer
#     who is watching the question it opened — sees the secret the first time
#     the operator votes honestly, and can cast as that operator on every later
#     question. A signature hands over a value that verifies ONE question;
#   * a captured bearer secret is replayable at will: against another question,
#     another round of the same question, another action (`revoke`, `override`),
#     and with the vote flipped from `approve` to `deny`. A signature is over
#     the question's own binding TOGETHER WITH the action and the vote, so a
#     captured one re-presents exactly the cast it already was, which the graph
#     has already counted and refuses as a duplicate.
#
# It does NOT answer the third, and that is the one #979 is about: a caller
# holding two operators' PRIVATE keys signs twice and satisfies a two-of-M rule
# from one session, and the graph honestly reads as two principals because, to
# this boundary, it was. Signing moves the thing that must be held from a value
# the session can capture to one it never sees; it does not make the count a
# count of people. That needs the per-caller authenticated transport, where the
# N arrive on N connections and the count is of the connections.

#: Domain separator for the principal id recorded in the decision graph. The id
#: is a hash OF the credential digest, never the digest itself: the graph is
#: durable, readable and copied into audits, and a row carrying the verifier
#: would let a reader of the record forge future casts.
_PRINCIPAL_DOMAIN = b"revl.quorum.principal\x00"

#: The same, for a cast-signing key. A different domain so a bearer digest and a
#: public key can never collide into one principal id by accident; the public key
#: is not a secret, and the hash is for uniformity and length, not secrecy.
_SIGNER_DOMAIN = b"revl.quorum.signer\x00"

#: Domain separator and version for the bytes a cast SIGNS. It is a version of
#: the signed payload's SHAPE: a verifier that added a field without moving this
#: would accept a signature made over the old shape as one over the new.
CAST_DOMAIN = b"revl.quorum.cast.v1\x00"

#: The suite a signed cast uses: deterministic ECDSA over NIST P-256 with
#: SHA-256 (`revl.tee_quote`, RFC 6979), raw `R || S` hex on the wire.
CAST_ALG = "p256"


def cast_message(binding: dict) -> bytes:
    """The exact bytes a cast signs: THIS act, on THIS question, and nothing
    else.

    Every field is load-bearing, and each one is a replay the signature refuses:

      * `requestId` / `hash` / `candidateHash` / `component` / `kind` — the
        question. A signature made for one crossing does not verify against
        another, so a captured proof is not a credential for the next question;
      * `round` — the ticket's round. A ticket hash is the identity of a
        QUESTION and repeats verbatim whenever the same crossing is attempted
        again (`Session._issue_ticket`), so without the round a proof from the
        first asking would answer every later asking of the same call;
      * `action` — `vote`, `escalate`, `revoke` or `override`. An operator who
        signed a vote did not thereby sign an escalation, and the emergency path
        is a different authority (it is gated by a different verb), so it must be
        a different signature;
      * `vote` — `approve` or `deny`. A proof captured from a denial cannot be
        re-presented as an approval;
      * `asToken` — the identity the cast counts for. A proof is not
        transferable to another name even if that name shares the key, so the
        row the graph writes is the row the signer meant to write.

    The bytes are the domain separator followed by the canonical JSON of those
    fields, which is the same canonicalisation the receipt digest uses: sorted
    keys, no insignificant whitespace, so two readers of one question agree on
    the message byte for byte."""
    return CAST_DOMAIN + _canon({
        "requestId": binding.get("requestId"),
        "hash": binding.get("hash"),
        "candidateHash": binding.get("candidateHash"),
        "component": binding.get("component"),
        "kind": binding.get("kind"),
        "round": binding.get("round"),
        "action": binding.get("action"),
        "vote": binding.get("vote"),
        "asToken": binding.get("asToken"),
    }).encode("utf-8")


def sign_cast(private_key: int, binding: dict) -> str:
    """Sign one cast, returning the raw `R || S` hex a caller puts in `asProof`.

    The signer lives beside the verifier deliberately. An operator needs SOME
    canonical way to produce the proof, and a protocol whose only implementation
    of the signed message is inside the verifier is a protocol every client has
    to guess at — the guesses disagree, and the disagreements are read as
    refusals rather than as the interop bug they are. It takes the private scalar
    and never a file: where an operator's private key lives is the operator's
    problem and not this module's, and a helper that read keys off disk would
    quietly become the place they get stored."""
    from ..tee_quote import CURVE_P256, ecdsa_sign
    return ecdsa_sign(CURVE_P256, private_key, cast_message(binding)).hex()


def _verify_cast(public_key_hex: str, binding: dict, proof: str) -> bool:
    """Does `proof` verify as a signature over `binding` under `public_key_hex`?

    Every malformed input answers False rather than raising: a caller's proof is
    a peer record and a bad one is a refusal, not a crash. The one thing that
    could raise — a public key that is not on the curve — cannot reach here,
    because the profile parser validated the point when it loaded
    (`revl.mcp.operator._parse_sign_key`)."""
    from ..tee_quote import CURVE_P256, QuoteFormatError, ecdsa_verify
    try:
        raw = bytes.fromhex(str(proof).strip())
    except ValueError:
        return False
    try:
        return ecdsa_verify(CURVE_P256, bytes.fromhex(public_key_hex),
                            cast_message(binding), raw)
    except QuoteFormatError:
        return False


@dataclass(frozen=True)
class Cast:
    """A cast whose identity is BOUND: who it counts for, which principal
    supplied it, and what bound it.

    `how` is `"session"` (the serve-time operator binding) or `"credential"` (a
    proven vote credential). It is recorded on the decision graph so an audit
    reads what each cast rested on rather than assuming."""

    voter: str
    principal: str
    how: str


@dataclass(frozen=True)
class UnboundCast:
    """A cast whose identity could not be bound, and therefore is refused.

    `reason` is the machine token written to the `quorum-refused` row;
    `asserted` is the name the caller CLAIMED, recorded so the graph shows who
    was attempted rather than a blank."""

    reason: str
    message: str
    asserted: str


def _principal_of_credential(digest: str) -> str:
    return "cred:" + hashlib.sha256(
        _PRINCIPAL_DOMAIN + digest.encode("utf-8")).hexdigest()[:16]


def _principal_of_signer(public_key_hex: str) -> str:
    return "sign:" + hashlib.sha256(
        _SIGNER_DOMAIN + public_key_hex.encode("utf-8")).hexdigest()[:16]


def _lifetime_refusal(operator, now_ms):
    """Is this operator's credential still in force at `now_ms`, or is the cast
    refused before its proof is even looked at?

    Both checks come BEFORE the credential is verified, so a revoked operator's
    still-valid signature and a still-known secret are refused on the same
    ground: the question is not whether the holder can prove the credential, it
    is whether the credential still binds anything.

    A declared window with no clock to evaluate it against also refuses. That is
    the fail-closed reading and the only safe one: the alternative is to admit
    while unable to say whether the grant had already lapsed, which is exactly
    the state an expiry exists to make impossible."""
    if getattr(operator, "revoked", False):
        return UnboundCast(
            "revoked-credential",
            f"the operator profile marks `{operator.token}`'s vote credential "
            f"REVOKED, so no cast is attributed to it: a revoked identity is "
            f"refused whether or not the holder can still prove the credential "
            f"(roadmap item 471, issue #979, fail closed)",
            operator.token)
    not_after = getattr(operator, "not_after", None)
    if not_after is None:
        return None
    if now_ms is None:
        return UnboundCast(
            "expired-credential",
            f"`{operator.token}`'s vote credential is declared valid only until "
            f"{not_after}, and this cast carries no clock reading to evaluate "
            f"that against. A credential whose window cannot be checked is not "
            f"in force (roadmap item 471, issue #979, fail closed)",
            operator.token)
    if now_ms > not_after:
        return UnboundCast(
            "expired-credential",
            f"`{operator.token}`'s vote credential lapsed at {not_after} and it "
            f"is now {now_ms}: an expired credential binds no cast. Issue a "
            f"fresh one and update the operator profile (roadmap item 471, "
            f"issue #979, fail closed)",
            operator.token)
    return None


def resolve_cast(*, as_token, as_secret=None, bound, registry,
                 as_proof=None, binding=None, now_ms=None):
    """Bind one cast's identity, or refuse it.

    Returns a :class:`Cast` when the identity is bound and an
    :class:`UnboundCast` when it is not. FAIL CLOSED is the whole contract:
    every path that cannot prove who is casting returns `UnboundCast`, and there
    is no branch that falls back to believing `as_token`.

    `bound` is the session's :class:`revl.mcp.operator.Operator` (or None when
    no profile is bound); `registry` is the whole
    :class:`revl.mcp.operator.OperatorRegistry` the session was served with, which
    is what a credential is checked against.

    `as_proof` is the raw `R || S` hex of a signature over `binding` (see
    :func:`cast_message`), `binding` the question-and-act this cast is for, and
    `now_ms` the session clock the credential's own lifetime is checked against.
    All three are optional in the SIGNATURE only so an existing bearer-credential
    call site keeps working unchanged; a cast against an operator that declares a
    signing key needs all of them and refuses without them.

    The ways a cast is refused, each named so the refusal is actionable:

      * `unnamed-credential` - a credential or a proof with no `asToken` beside
        it. The session will not search the profile for whichever identity a
        secret happens to open: a cast says who it is for, and the credential
        proves that claim;
      * `unbound-identity` - a name other than the session's own with no
        operator profile to check it against. A session served without a profile
        has exactly one identity, so a second one cannot be bound at all;
      * `unknown-operator` / `unkeyed-identity` - the profile does not carry the
        named operator, or carries it with no declared credential of either
        kind. Neither can be proven, so neither is believed;
      * `unproven-identity` - a name the profile knows, presented with a missing
        or wrong bearer credential;
      * `unsigned-cast` - a name whose profile entry binds casts BY SIGNATURE,
        presented with no proof (or with a bearer secret instead). There is no
        downgrade here: an operator that declares a signing key cannot be cast
        for by presenting a secret, because if it could then declaring the key
        would bound nothing;
      * `unproven-signature` - a proof that does not verify over this question's
        binding under the operator's declared key, is malformed, or names an
        operator that declares no signing key to check it against;
      * `unbound-question` - a proof with no question binding to verify it
        against. A signature is only ever a signature OVER something, and a
        verifier with nothing to verify against cannot be made to admit;
      * `revoked-credential` / `expired-credential` - the credential is no
        longer in force, checked before the proof is looked at.
    """
    bound_token = getattr(bound, "token", None) if bound is not None else None
    secret = as_secret if as_secret not in (None, "") else None
    proof = as_proof if as_proof not in (None, "") else None
    def _session_cast():
        """The serve-time binding, or the refusal that says it no longer holds.

        The binding is process configuration rather than a credential, but a
        REVOKED operator is a revoked operator: if the profile says this identity
        no longer acts, the session's own binding to it does not resurrect it.
        It cannot supply one of the N anyway - on one session the bound operator
        is the proposer and separation of duties excludes it - but it can still
        escalate, revoke and override, which are the acts revocation most needs
        to reach."""
        if bound is not None:
            lapsed_session = _lifetime_refusal(bound, now_ms)
            if lapsed_session is not None:
                return lapsed_session
        return Cast(bound_token or "", f"session:{bound_token or ''}", "session")

    if as_token is None or as_token == "":
        if secret is not None or proof is not None:
            return UnboundCast(
                "unnamed-credential",
                "a vote credential was presented with no `asToken` beside it: a "
                "cast names the operator it counts for and the credential proves "
                "that name, so the session will not resolve an identity by "
                "searching the profile for whichever secret matches (roadmap "
                "item 471, issue #979)",
                bound_token or "")
        return _session_cast()

    if as_token == bound_token and secret is None and proof is None:
        # naming the session's own identity adds nothing to assert: it is the
        # serve-time binding either way.
        return _session_cast()

    if registry is None:
        return UnboundCast(
            "unbound-identity",
            f"`{as_token}` cannot be bound on this session: it is served with no "
            f"operator profile, so the only identity it has is its own bound "
            f"operator (`{bound_token or 'none'}`) and a second one cannot be "
            f"proven. A quorum counts DISTINCT PRINCIPALS, and a name with "
            f"nothing behind it is not one (roadmap item 471, issue #979, fail "
            f"closed)",
            as_token)
    operator = registry.get(as_token)
    if operator is None:
        return UnboundCast(
            "unknown-operator",
            f"the operator profile declares no operator `{as_token}`, so a cast "
            f"attributed to it cannot be proven (known: "
            f"{', '.join(sorted(registry.operators)) or 'none'}) (roadmap item "
            f"471, issue #979, fail closed)",
            as_token)
    lapsed = _lifetime_refusal(operator, now_ms)
    if lapsed is not None:
        return lapsed
    if operator.sign_key:
        # A signing key is not a stronger OPTION beside the secret, it REPLACES
        # it. An operator that declares one is cast for by proof or not at all,
        # which is why `asSecret` here is a refusal and not a fallback.
        if proof is None:
            return UnboundCast(
                "unsigned-cast",
                f"operator `{as_token}` binds its casts by SIGNATURE, so this "
                f"cast must carry `asProof`: the raw R||S hex of a P-256 "
                f"signature over this question's binding "
                f"(`revl.mcp.quorum.cast_message`). "
                + ("A bearer `asSecret` does not stand in for it - a secret that "
                   "crossed the wire once is replayable against every later "
                   "question, which is the property the signing key exists to "
                   "remove. " if secret is not None else "")
                + "(roadmap item 471, issue #979, fail closed)",
                as_token)
        if not binding:
            return UnboundCast(
                "unbound-question",
                f"a signed cast attributed to `{as_token}` arrived with no "
                f"question binding to verify it against. A signature proves a "
                f"statement about ONE question, and a verifier handed no "
                f"question has nothing to check, so it refuses rather than "
                f"treating the proof as self-evident (roadmap item 471, issue "
                f"#979, fail closed)",
                as_token)
        if not _verify_cast(operator.sign_key, binding, proof):
            return UnboundCast(
                "unproven-signature",
                f"the proof presented for `{as_token}` is not a valid P-256 "
                f"signature over THIS question's binding under the key the "
                f"operator profile declares. A signature made for another "
                f"question, another round, another act, or the other vote is a "
                f"signature about something else, and is refused here exactly as "
                f"a forged one is (roadmap item 471, issue #979, fail closed)",
                as_token)
        return Cast(as_token, _principal_of_signer(operator.sign_key),
                    "signature")
    if proof is not None:
        return UnboundCast(
            "unproven-signature",
            f"a signed cast was attributed to `{as_token}`, but the operator "
            f"profile declares no cast-signing key for it, so there is nothing "
            f"to verify the proof against. Give it an `operator {as_token} sign "
            f"p256:<hex>` line (roadmap item 471, issue #979, fail closed)",
            as_token)
    if not operator.vote_key:
        return UnboundCast(
            "unkeyed-identity",
            f"operator `{as_token}` declares no vote credential, so a cast "
            f"attributed to it rests on the caller's word alone. Give it an "
            f"`operator {as_token} sign p256:<public key hex>` line and sign the "
            f"question's binding into `asProof` - or, for the weaker bearer "
            f"form, an `operator {as_token} key sha256:<digest>` line presented "
            f"as `asSecret` (roadmap item 471, issue #979, fail closed)",
            as_token)
    if secret is None:
        return UnboundCast(
            "unproven-identity",
            f"a cast attributed to `{as_token}` must present that operator's "
            f"vote credential as `asSecret`: the name alone is asserted by the "
            f"caller, and a quorum that counts asserted names counts one "
            f"operator N times (roadmap item 471, issue #979, fail closed)",
            as_token)
    presented = hashlib.sha256(str(secret).encode("utf-8")).hexdigest()
    if not hmac.compare_digest(presented, operator.vote_key):
        return UnboundCast(
            "unproven-identity",
            f"the credential presented for `{as_token}` does not match the one "
            f"the operator profile declares, so the cast is not that operator's "
            f"(roadmap item 471, issue #979, fail closed)",
            as_token)
    return Cast(as_token, _principal_of_credential(operator.vote_key),
                "credential")


# --------------------------------------------------------------------------- #
# The admission receipt                                                       #
# --------------------------------------------------------------------------- #
def _binding(decision: dict, entry: dict) -> dict:
    """What the decision, and therefore the receipt, is BOUND to.

    Every axis the item names: the ticket hash (which carries the arguments
    digest), the reach-closure candidate hash (the plan and the resource target
    the closure reached), the crossing component, the ticket kind, the round, the
    capabilities the closure reaches, and the deadline the votes were bound to.
    A receipt whose binding differs in any one of them is a receipt for a
    different question — which is what makes it worth hashing."""
    return {
        "requestId": decision["requestId"],
        "hash": decision["hash"],
        "candidateHash": decision["candidateHash"],
        "component": decision["component"],
        "ticketKind": decision.get("kind"),
        "round": decision["round"],
        "capabilities": list(decision.get("capabilities") or ()),
        "expiresAt": decision.get("expiresAt"),
        "grantedAt": entry.get("grantedAt"),
        "argsDigest": entry.get("argsDigest"),
        "session": entry.get("session"),
    }


def _vote_rows(decision: dict) -> list:
    """Every counted vote, in vote-id order so the list is canonical (the digest
    is over it, and a dict iteration order must not change a receipt's identity)."""
    return sorted(
        ({"voteId": row["voteId"], "voter": row["voter"], "vote": row["vote"],
          "at": row["at"], "round": row["round"]}
         for row in decision["votes"].values()),
        key=lambda row: row["voteId"])


def _refusal_rows(records: list, request_id: str,
                  through_index: int | None = None) -> list:
    """Every cast or attempt this question REFUSED, from the graph's own
    `quorum-refused` rows. The refusals are part of the decision, not noise
    beside it: "two approvers said yes" reads differently when four were turned
    away first, and an audit that cannot see the turned-away casts cannot tell a
    clean quorum from a probed one.

    `through_index` bounds the set to the refusals that are part of THIS decision
    - every refusal up to and including the row that minted the authority the
    receipt spent, i.e. :func:`_spend_bound`. A refusal is a fact about the graph
    and stays on the graph, but one written AFTER the decision closed - a late
    vote, a repeat probe - is a fact about the question's afterlife, not about
    the decision that admitted, and folding it in would make a correctly spent
    admission re-verify as forged the moment somebody probed it. `None` keeps the
    whole history, which is what the live reader wants.

    The bound is a POSITION in the record list and never a clock reading. The
    session clock is ratcheted to a high-water floor, so two acts a microsecond
    apart can carry the SAME `at`; a `row["at"] <= spend.consumedAt` test would
    then fold a refusal that happened strictly after the spend back into the
    receipt, which is the very defect this bounds. Positions have no ties."""
    return [
        {"action": row.get("action"), "reason": row.get("reason"),
         "voter": row.get("voter"), "counted": row.get("counted")}
        for index, row in enumerate(records)
        if row.get("record") == "quorum-refused"
        and row.get("requestId") == request_id
        and (through_index is None or index <= through_index)
    ]


def _spend_bound(records: list, request_id: str) -> int | None:
    """Where the decision ENDS and the question's afterlife begins: the position
    of the `approval-granted` row that minted the authority this receipt's spend
    consumed. It is written after the row that decided the question and before the
    spend, so bounding the refusals through it keeps every cast the decision saw
    and drops every act that came after - a late vote on the spent question, the
    probe an auditor runs a week later.

    Re-derived, never stored: it is a function of the durable rows and the entry
    the receipt already names, so there is no second copy of the bound for a
    forger to widen and none for the mint and the verifier to disagree about.

    `None` (no grant row for this request id) is unreachable for a receipt -
    `_mint_ticket_entry` writes the row for every authority that can be spent, so
    a receipt whose entry has no row is a receipt for a spend nobody made."""
    for index, row in enumerate(records):
        if row.get("record") == "approval-granted" \
                and row.get("requestId") == request_id:
            return index
    return None


def _counted(decision: dict) -> int:
    return sum(1 for row in decision["votes"].values()
               if row["vote"] == "approve")


def receipt_body(decision: dict, entry: dict, records: list) -> dict:
    """The receipt without its digest: the binding, the rule as written, and the
    decision graph in full. Pure over its inputs, so :func:`verify_receipt` can
    rebuild it from the durable rows and compare byte for byte.

    The refusal set is bounded by the decision rather than by the whole history
    (see :func:`_refusal_rows` and :func:`_spend_bound`), and the bound is a
    POSITION re-derived from `records` and `entry` on every call - the mint and
    the verifier compute the same one from the same rows, so the receipt stays a
    derivation of the graph rather than a second copy of it."""
    body = {
        "kind": RECEIPT_KIND,
        "version": RECEIPT_VERSION,
        "binding": _binding(decision, entry),
        "rule": {
            "text": decision["rule"],
            "require": decision["require"],
            "approvers": list(decision["approvers"]),
            "proposer": decision["proposer"],
        },
        "decision": {
            "outcome": decision["outcome"],
            "satisfiedBy": decision["satisfiedBy"],
            "counted": _counted(decision),
            "openedAt": decision["openedAt"],
            "resolvedAt": decision["resolvedAt"],
            "votes": _vote_rows(decision),
            "refusals": _refusal_rows(
                records, decision["requestId"],
                _spend_bound(records, entry["requestId"])),
        },
    }
    if decision.get("override"):
        body["decision"]["override"] = dict(decision["override"])
    return body


def build_receipt(decision: dict, entry: dict, records: list, *,
                  consumed_at: int | None) -> dict:
    """Mint the admission receipt for the spend of `entry` under `decision`.

    Called at the crossing, after the durable `approval-consumed` record and
    before the fire, so the receipt names an authority that was actually spent.
    `consumed_at` is the spend's own clock reading; it is inside the digest, so a
    receipt cannot be re-dated. The refusal set is bounded by the decision's own
    end (the `approval-granted` row for this request id), so a later refusal
    cannot rewrite what this receipt says."""
    body = receipt_body(decision, entry, records)
    body["spend"] = {"requestId": entry["requestId"], "consumedAt": consumed_at}
    return {**body, "digest": _sha(_canon(body))}


def verify_receipt(receipt: dict, decision: dict | None,
                   entry: dict | None, records: list) -> dict:
    """Re-derive `receipt` from the durable decision graph and say whether it
    holds, as `{"ok": bool, "reasons": [...]}`.

    Fail-closed and specific. Each reason names the axis that did not match, so a
    verdict is actionable rather than a bare no:

      * `unknown-kind` / `unknown-version` - not a receipt this verifier reads;
      * `no-decision` - the graph carries no question under the receipt's
        request id, so there is nothing to check it against. A receipt that names
        a decision nobody has is refused, never assumed;
      * `no-spend` - the receipt claims no spend, so it is not an ADMISSION
        receipt;
      * `digest` - the receipt's own digest does not cover its own body: a field
        was edited after the mint;
      * `binding`, `rule`, `decision` - the body disagrees with the rows it was
        derived from, with the mismatching axis named. This is the one that
        catches a receipt re-pointed at another candidate: the binding carries
        the candidate hash, and the graph's own row carries the real one."""
    reasons: list[str] = []
    if receipt.get("kind") != RECEIPT_KIND:
        reasons.append(f"unknown-kind: {receipt.get('kind')!r}")
    if receipt.get("version") != RECEIPT_VERSION:
        reasons.append(f"unknown-version: {receipt.get('version')!r}")
    spend = receipt.get("spend")
    if not isinstance(spend, dict) or not spend.get("requestId"):
        reasons.append("no-spend: the receipt names no consumed authority")
    if reasons:
        return {"ok": False, "reasons": reasons}

    claimed = dict(receipt)
    digest = claimed.pop("digest", None)
    if digest != _sha(_canon(claimed)):
        reasons.append("digest: the receipt does not hash to its own body")
    if decision is None or entry is None:
        reasons.append(
            f"no-decision: the decision graph carries no question under "
            f"{receipt.get('binding', {}).get('requestId')!r}")
        return {"ok": False, "reasons": reasons}

    rebuilt = receipt_body(decision, entry, records)
    for axis in ("binding", "rule", "decision"):
        if _canon(rebuilt.get(axis)) != _canon(claimed.get(axis)):
            reasons.append(
                f"{axis}: the receipt disagrees with the decision graph")
    if spend.get("requestId") != entry["requestId"]:
        reasons.append("binding: the spend names another authority")
    return {"ok": not reasons, "reasons": reasons}


# --------------------------------------------------------------------------- #
# The operator verbs                                                          #
# --------------------------------------------------------------------------- #
#
# Each adapter is deliberately thin. The authority decisions - who may escalate,
# who may revoke, that an override states a reason, that a decided question is
# not re-decidable - are the session protocol's, recorded in the decision graph
# before they are raised (Slice 1). What the transport adds is the argument shape
# and the operator verb gate, and nothing else: a second copy of a membership
# check here could disagree with the one that writes the record.

def _require_hash(arguments: dict, what: str) -> str:
    """The ticket hash every question-scoped verb takes. A missing hash is a
    usage error refused before the session is touched, so no decision graph is
    opened for a call that names no question."""
    ticket_hash = arguments.get("hash")
    if not ticket_hash:
        raise ValueError(
            f"provide `hash` - the ticket hash from the approvalRequired "
            f"response. {what} acts on ONE outstanding multi-party question "
            f"(roadmap item 471)")
    return ticket_hash


def escalate(session, arguments: dict) -> dict:
    """`revl_escalate`: hand a stalled multi-party question up, closing its vote
    path (item 471). Gated by the `approve` verb: an operator who may answer the
    question may also say it cannot be answered as written. Escalation only ever
    NARROWS authority - the remaining path is the separately granted override -
    so it needs no authority of its own beyond the one to vote."""
    ticket_hash = _require_hash(arguments, "escalation")
    return session.escalate_ticket(
        ticket_hash, reason=arguments.get("reason"),
        as_token=arguments.get("asToken"),
        as_secret=arguments.get("asSecret"),
        as_proof=arguments.get("asProof"))


def revoke_question(session, arguments: dict) -> dict:
    """`revl_revoke`'s multi-party branch: withdraw a PENDING question (item
    471), as distinct from retiring an already-minted standing grant (item 379,
    the same tool's `capability`/`requestId` branches). The proposer withdraws
    what it asked for; a named approver closing it is a veto. Gated by `approve`,
    the same authority as saying yes."""
    ticket_hash = _require_hash(arguments, "revoking a pending question")
    return session.revoke_ticket(
        ticket_hash, reason=arguments.get("reason"),
        as_token=arguments.get("asToken"),
        as_secret=arguments.get("asSecret"),
        as_proof=arguments.get("asProof"))


def override(session, arguments: dict) -> dict:
    """`revl_override`: admit a multi-party crossing WITHOUT its count (item
    471), the emergency path.

    Gated by its own `override` operator verb, never by `approve`. That is the
    whole point of the verb: an operator trusted to cast one of N votes is not
    thereby trusted to stand in for all of them, so the authority to break the
    glass is a separate address in the operator profile. The record says
    `satisfiedBy: "override"` with the count it actually had, which is below
    `require` by construction, and carries `selfOverride` when the operator who
    broke the glass is the one who proposed the crossing - so an audit reads an
    override as an override, and a self-override as a self-override.

    A missing reason is refused by the session and recorded as a refusal: an
    override nobody stated a reason for is an unattributable act."""
    ticket_hash = _require_hash(arguments, "an override")
    return session.override_ticket(
        ticket_hash, reason=arguments.get("reason"),
        as_token=arguments.get("asToken"),
        as_secret=arguments.get("asSecret"),
        as_proof=arguments.get("asProof"))


def decision_report(session, arguments: dict) -> dict:
    """`revl_quorum`: read the decision graph of one question, and the admission
    receipt when the authority it minted has been spent (item 471).

    Read-only and ungated - it decides nothing and mints nothing, so it reaches
    no privileged operation. This is the "auditable after the fact" half of the
    item: the graph names who was asked, who voted, who was turned away and why,
    and how the question closed; the receipt is the hash-bound artifact for the
    one crossing the decision authorized, re-derived from those same rows by
    `verify` so a receipt that disagrees with the record is refused rather than
    reported as fact."""
    ticket_hash = _require_hash(arguments, "reading a decision graph")
    report = dict(session.quorum_state(ticket_hash))
    receipt = session.quorum_receipt(ticket_hash)
    if receipt is not None:
        report["receipt"] = receipt
        if arguments.get("verify"):
            report["verification"] = session.verify_quorum_receipt(receipt)
    return report

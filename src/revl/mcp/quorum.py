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


def _refusal_rows(records: list, request_id: str) -> list:
    """Every cast or attempt this question REFUSED, from the graph's own
    `quorum-refused` rows. The refusals are part of the decision, not noise
    beside it: "two approvers said yes" reads differently when four were turned
    away first, and an audit that cannot see the turned-away casts cannot tell a
    clean quorum from a probed one."""
    return [
        {"action": row.get("action"), "reason": row.get("reason"),
         "voter": row.get("voter"), "counted": row.get("counted")}
        for row in records
        if row.get("record") == "quorum-refused"
        and row.get("requestId") == request_id]


def _counted(decision: dict) -> int:
    return sum(1 for row in decision["votes"].values()
               if row["vote"] == "approve")


def receipt_body(decision: dict, entry: dict, records: list) -> dict:
    """The receipt without its digest: the binding, the rule as written, and the
    decision graph in full. Pure over its inputs, so :func:`verify_receipt` can
    rebuild it from the durable rows and compare byte for byte."""
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
            "refusals": _refusal_rows(records, decision["requestId"]),
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
    receipt cannot be re-dated."""
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
        as_token=arguments.get("asToken"))


def revoke_question(session, arguments: dict) -> dict:
    """`revl_revoke`'s multi-party branch: withdraw a PENDING question (item
    471), as distinct from retiring an already-minted standing grant (item 379,
    the same tool's `capability`/`requestId` branches). The proposer withdraws
    what it asked for; a named approver closing it is a veto. Gated by `approve`,
    the same authority as saying yes."""
    ticket_hash = _require_hash(arguments, "revoking a pending question")
    return session.revoke_ticket(
        ticket_hash, reason=arguments.get("reason"),
        as_token=arguments.get("asToken"))


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
        as_token=arguments.get("asToken"))


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

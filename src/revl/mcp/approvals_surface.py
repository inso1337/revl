"""The read-only approval query surface (issue #2991).

Four things an approver could not see in one place before this module: the
outstanding tickets, what one ticket would let through, who asked and who may
answer, and how the session's approval decisions have gone over time. It is a
projection over the live session's own fields - never a second store and never a
write:

  * :func:`list_approvals` reads the outstanding-ticket table put together from
    the class map's ``_tickets``/``_ticket_rounds``, the per-operator asking
    book ``_ticket_pending_raisers``, the NOs in ``_ticket_refusals``, the
    multi-party decision graph in ``_quorums`` and the minted grants in
    ``_ledger``.
  * :func:`show_approval` renders ONE ticket: its body (the crossing, its
    capability reach and resource bindings), the policy rules that answer for it,
    the binding hashes, the decision graph and grant provenance, and the next
    actions the surface can honestly offer. ``diff`` adds the blast-radius
    projection - what will fire, whether it has a checked inverse, what the
    closure reaches and what the ticket discloses about secrets.
  * :func:`stats` folds this session's in-memory ``_approval_records`` together
    with the durable records read back by ``_wal_ledger_records`` (which outlive
    the session and may carry earlier sessions sharing the same WAL), counting
    states and decision times and listing overrides/escalations.

The surface is deliberately HONEST about what it does not have. A pending
single-party ticket records an asking round, not a timestamp, and no expiry at
all - the ttl bounds the approval a yes mints, not the question - so ``ageMs``
and ``expiryRemainingMs`` are ``None`` with a named reason rather than an
invented number. There is no model recommendation for approvals anywhere in the
tree, so ``advice.model`` is ``None`` with that stated, and the advice list is
the deterministic next-step guidance the decision facts support.

Read-only means read-only: nothing here mutates the session, the ledger, the
decision graph or the WAL. The one session call it makes is
``_wal_ledger_records()``, whose whole contract is "read the log, never write
it".
"""

from __future__ import annotations

import time

#: The states the surface can report, in the order a ticket moves through them.
#: One row gets exactly one of these, derived from the session's own fields.
STATE_PENDING = "pending"
STATE_GRANTED = "granted"
STATE_CONSUMED = "consumed"
STATE_EXPIRED = "expired"
STATE_REVOKED = "revoked"
STATE_REFUSED = "refused"
STATE_SATISFIED = "satisfied"
STATE_DENIED = "denied"
STATE_ESCALATED = "escalated"
STATE_CLOSED = "closed"

_MODEL_ADVICE_ABSENT = (
    "no model recommendation is recorded for approvals; this surface renders "
    "the decision facts and the deterministic next actions, never a model's "
    "opinion presented as advice")

_SINGLE_PARTY_AGE_ABSENT = (
    "a pending single-party ticket records the asking round, not a timestamp, "
    "so its age is not derivable; the decision graph's `openedAt` is reported "
    "for a multi-party question")
_SINGLE_PARTY_EXPIRY_ABSENT = (
    "a pending question carries no ttl of its own; the policy ttl bounds the "
    "approval a yes mints, not the asking, so there is no expiry to count down")


# ---------------------------------------------------------------------------
# small read helpers
# ---------------------------------------------------------------------------

def _now(session) -> int:
    clock = getattr(session, "_now_ms", None)
    if callable(clock):
        try:
            return int(clock())
        except Exception:  # noqa: BLE001 - a read surface degrades, never crashes
            pass
    return int(time.time() * 1000)


def _session_id(session) -> str | None:
    value = getattr(session, "_session_id", None)
    return value if isinstance(value, str) else None


def _tickets(session) -> dict:
    table = getattr(session, "_tickets", None)
    return table if isinstance(table, dict) else {}


def _rounds(session) -> dict:
    table = getattr(session, "_ticket_rounds", None)
    return table if isinstance(table, dict) else {}


def _pending_raisers(session) -> dict:
    table = getattr(session, "_ticket_pending_raisers", None)
    return table if isinstance(table, dict) else {}


def _refusals(session) -> dict:
    table = getattr(session, "_ticket_refusals", None)
    return table if isinstance(table, dict) else {}


def _quorums(session) -> dict:
    table = getattr(session, "_quorums", None)
    return table if isinstance(table, dict) else {}


def _ledger(session) -> list:
    table = getattr(session, "_ledger", None)
    return table if isinstance(table, list) else []


def _approval_records(session) -> list:
    records = getattr(session, "_approval_records", None)
    return records if isinstance(records, list) else []


def _quorum_request_id(session, ticket_hash: str) -> str:
    helper = getattr(session, "_quorum_request_id", None)
    if callable(helper):
        try:
            return helper(ticket_hash)
        except Exception:  # noqa: BLE001
            pass
    round_ = _rounds(session).get(ticket_hash, 1)
    return ticket_hash if round_ <= 1 else f"{ticket_hash}#r{round_}"


def _rule_for(session, capability: str):
    sandbox = getattr(session, "sandbox", None)
    lookup = getattr(sandbox, "approval_rule_for", None) if sandbox is not None \
        else None
    if not callable(lookup):
        return None
    try:
        return lookup(capability)
    except Exception:  # noqa: BLE001
        return None


def _effect(ticket: dict) -> dict:
    """The crossing a ticket asks about, in the terms an approver reads: what is
    called, and whether it is a call or an activation body."""
    component = ticket.get("component")
    key = ticket.get("key")
    method = ticket.get("method")
    kind = ticket.get("kind")
    if key is not None and method is not None:
        display = f"{key}.{method}"
    else:
        display = f"activation of {component}" if component else "(unknown effect)"
    return {"component": component, "key": key, "method": method,
            "kind": kind, "argsDigest": ticket.get("argsDigest"),
            "display": display}


def _expired(entry: dict, now: int) -> bool:
    expires = entry.get("expiresAt")
    return expires is not None and now > expires


def _entry_state(entry: dict, now: int) -> str:
    if entry.get("revoked"):
        return STATE_REVOKED
    if entry.get("consumed"):
        return STATE_CONSUMED
    if _expired(entry, now):
        return STATE_EXPIRED
    return STATE_GRANTED


def _entries_for(session, ticket_hash: str) -> list:
    """Every minted grant for this ticket, newest last. The ledger is append-only
    and a ticket hash names a QUESTION, so there can be one entry per answer
    round (and, since issue #3216, one per operator whose asking was answered)."""
    entries = [entry for entry in _ledger(session)
               if entry.get("hash") == ticket_hash]
    return sorted(entries, key=lambda e: (e.get("grantedAt") or 0,
                                          str(e.get("requestId"))),
                  reverse=True)


def _quorum_projection(session, ticket: dict) -> dict | None:
    """The multi-party decision graph for this ticket's current round, shaped for
    a reader. ``None`` when no graph was opened (a single-party crossing)."""
    ticket_hash = ticket.get("hash")
    if not ticket_hash:
        return None
    record = _quorums(session).get(_quorum_request_id(session, ticket_hash))
    if record is None:
        return None
    request_id = record.get("requestId")
    refusals = [
        {"action": row.get("action"), "reason": row.get("reason"),
         "voter": row.get("voter"), "at": row.get("at")}
        for row in _approval_records(session)
        if row.get("record") == "quorum-refused"
        and row.get("requestId") == request_id]
    return {
        "requestId": request_id,
        "round": record.get("round"),
        "rule": record.get("rule"),
        "require": record.get("require"),
        "approvers": list(record.get("approvers") or []),
        "proposer": record.get("proposer"),
        "openedAt": record.get("openedAt"),
        "expiresAt": record.get("expiresAt"),
        "expiredAt": record.get("expiredAt"),
        "outcome": record.get("outcome"),
        "satisfiedBy": record.get("satisfiedBy"),
        "resolvedAt": record.get("resolvedAt"),
        "counted": len(record.get("votes") or {}),
        "outstanding": [name for name in record.get("approvers") or []
                        if name not in (record.get("votes") or {})],
        "votes": [dict(row) for row in (record.get("votes") or {}).values()],
        "refusals": refusals,
        **({"override": record["override"]}
           if record.get("override") else {}),
    }


def _policy(session, ticket: dict) -> dict:
    capabilities = list(ticket.get("capabilities") or [])
    rules = []
    for capability in capabilities:
        rule = _rule_for(session, capability)
        if rule is None:
            rules.append({"capability": capability, "rule": None,
                          "detail": "no approval rule covers this capability"})
            continue
        rules.append({
            "capability": capability,
            "ttlMs": getattr(rule, "ttl_ms", None),
            "require": getattr(rule, "require", 1),
            "approvers": list(getattr(rule, "approvers", ()) or ()),
            "rule": (rule.quorum_text()
                     if getattr(rule, "approvers", ()) else
                     f"requires approval for {capability}"),
        })
    return {
        "approvalPolicy": getattr(session, "approval_policy", None),
        "separation": bool(getattr(session, "approval_separation", False)),
        "recordValues": getattr(session, "approval_record_values", None),
        "rules": rules,
    }


def _bindings(session, ticket: dict, round_: int) -> dict:
    return {
        "hash": ticket.get("hash"),
        "candidateHash": ticket.get("candidateHash"),
        "argsDigest": ticket.get("argsDigest"),
        "component": ticket.get("component"),
        "key": ticket.get("key"),
        "method": ticket.get("method"),
        "kind": ticket.get("kind"),
        "round": round_,
        "session": _session_id(session),
        "closureComponents": list(ticket.get("closureComponents") or []),
        "classCCapabilities": list(ticket.get("classCCapabilities") or []),
    }


def _spends(session, request_ids) -> list:
    """The durable `approval-consumed` / `approval-emission` rows for the grants
    of this ticket, joined on `(requestId, use)` exactly as `revl.wal` joins
    them. This is the grant provenance once the authority has been spent."""
    wanted = {rid for rid in request_ids if rid}
    if not wanted:
        return []
    try:
        records = session._wal_ledger_records()
    except Exception:  # noqa: BLE001
        return []
    emitted = {}
    for record in records or []:
        if record.get("record") == "approval-emission":
            key = (record.get("requestId"), record.get("use"))
            emitted[key] = emitted.get(key, 0) + 1
    out = []
    for record in records or []:
        if record.get("record") != "approval-consumed":
            continue
        key = (record.get("requestId"), record.get("use"))
        if key[0] not in wanted:
            continue
        fired = emitted.get(key, 0) > 0
        if fired:
            emitted[key] -= 1
        out.append({"requestId": key[0], "use": key[1],
                    "scope": record.get("scope"), "fired": fired,
                    "seq": record.get("seq")})
    return out


def _grants(session, ticket_hash: str, now: int) -> list:
    out = []
    for entry in _entries_for(session, ticket_hash):
        out.append({
            "requestId": entry.get("requestId"),
            "round": entry.get("round"),
            "proposers": list(entry.get("proposers") or []),
            "grantedAt": entry.get("grantedAt"),
            "expiresAt": entry.get("expiresAt"),
            "consumed": bool(entry.get("consumed")),
            "revoked": bool(entry.get("revoked")),
            "state": _entry_state(entry, now),
            "expiryRemainingMs": (
                None if entry.get("expiresAt") is None
                else max(0, int(entry["expiresAt"]) - now)),
        })
    return out


def _state(session, ticket: dict, now: int) -> tuple[dict, str | None]:
    """``(state_block, detail)`` for one ticket. The state is derived in the same
    order the session's own decision path reads it: a held NO first (it refuses
    the re-issue before anything can cover it), then the multi-party graph, then
    the pending askings, then the minted grants."""
    ticket_hash = ticket.get("hash")
    refusal = _refusals(session).get(ticket_hash)
    if refusal:
        if refusal.get("estop"):
            return ({"state": STATE_REVOKED,
                     "by": refusal.get("by"),
                     "reason": refusal.get("reason"),
                     "at": refusal.get("at"),
                     "detail": "revoked by the operator E-Stop; the re-issue is "
                               "refused and asking again opens a new round"},
                    STATE_REVOKED)
        return ({"state": STATE_REFUSED, "by": refusal.get("by"),
                 "reason": refusal.get("reason"), "at": refusal.get("at"),
                 "detail": "an operator answered NO; the re-issue this ticket "
                           "holds is refused once and fires nothing"},
                STATE_REFUSED)
    quorum = _quorum_projection(session, ticket)
    if quorum is not None:
        if quorum.get("outcome") is not None:
            return ({"state": quorum["outcome"],
                     "satisfiedBy": quorum.get("satisfiedBy"),
                     "resolvedAt": quorum.get("resolvedAt"),
                     "detail": f"the multi-party question closed "
                               f"`{quorum['outcome']}`"},
                    quorum["outcome"])
        if quorum.get("expiredAt") is not None or (
                quorum.get("expiresAt") is not None
                and now > int(quorum["expiresAt"])):
            return ({"state": STATE_EXPIRED,
                     "openedAt": quorum.get("openedAt"),
                     "expiresAt": quorum.get("expiresAt"),
                     "detail": "the question's deadline passed with no decision"},
                    STATE_EXPIRED)
        return ({"state": STATE_PENDING, "openedAt": quorum.get("openedAt"),
                 "expiresAt": quorum.get("expiresAt"),
                 "expiryRemainingMs": (
                     None if quorum.get("expiresAt") is None
                     else max(0, int(quorum["expiresAt"]) - now)),
                 "counted": quorum.get("counted"),
                 "require": quorum.get("require"),
                 "detail": f"open to votes: {quorum.get('counted')} of "
                           f"{quorum.get('require')} counted"},
                STATE_PENDING)
    raisers = _pending_raisers(session).get(ticket_hash) or {}
    entries = _entries_for(session, ticket_hash)
    if raisers:
        # issue #3216: one question can be answered for one operator's asking
        # while another operator's byte-identical asking still waits. The
        # QUESTION is pending, and the answered grants are shown beside it.
        detail = "waiting for a yes; a pending question carries no expiry of its own"
        state_block = {"state": STATE_PENDING,
                       "raisers": [name for name in raisers if name],
                       "detail": detail}
        if entries:
            latest = entries[0]
            state_block["answeredGrants"] = len(entries)
            state_block["latestGrant"] = _entry_state(latest, now)
            state_block["detail"] = (
                "one operator's asking is still waiting while "
                f"{len(entries)} grant(s) stand for other askings")
        return state_block, STATE_PENDING
    if entries:
        latest = entries[0]
        state = _entry_state(latest, now)
        detail = {
            STATE_GRANTED: "a yes is minted and not yet spent; re-issuing the "
                            "crossing fires it once",
            STATE_CONSUMED: "the yes was spent at the crossing",
            STATE_REVOKED: "the yes was withdrawn before it was spent",
            STATE_EXPIRED: "the yes lapsed before it was spent",
        }.get(state, state)
        return ({"state": state, "expiresAt": latest.get("expiresAt"),
                 "grantedAt": latest.get("grantedAt"),
                 "expiryRemainingMs": (
                     None if latest.get("expiresAt") is None
                     else max(0, int(latest["expiresAt"]) - now)),
                 "detail": detail}, state)
    return ({"state": STATE_CLOSED,
             "detail": "no asking is pending and no grant stands for this "
                       "ticket"}, STATE_CLOSED)


def _advice(session, ticket: dict, state: dict, *,
            quorum: dict | None, grants: list) -> dict:
    """The deterministic next actions the facts support. Never a model opinion:
    ``model`` is always ``None`` and says why."""
    h = ticket.get("hash")
    actions = []
    raisers = [name for name in
               (_pending_raisers(session).get(h) or {}) if name]
    if state["state"] == STATE_PENDING and quorum is not None:
        actions.append({
            "verb": "revl_approve", "arguments": {"hash": h, "vote": "approve"},
            "why": "cast one approver's vote on this multi-party question; a "
                   "vote must be cast by an operator the rule names, with the "
                   "credential the profile declares"})
        actions.append({
            "verb": "revl_override",
            "arguments": {"hash": h, "reason": "<why the count is bypassed>"},
            "why": "emergency path, and only for an operator granted the "
                   "`override` verb: admit the crossing without its count, "
                   "recorded as an override and never as a quorum"})
        actions.append({
            "verb": "revl_revoke", "arguments": {"hash": h},
            "why": "withdraw the question; the votes stop counting and no "
                   "approval is minted"})
    elif state["state"] == STATE_PENDING:
        approve_args = {"hash": h}
        if raisers:
            approve_args["proposer"] = raisers[0]
        actions.append({
            "verb": "revl_approve", "arguments": approve_args,
            "why": "mint the single-use yes this question asks for; the "
                   "identical re-issue then fires once"
                   + (" and only the named proposer's asking is answered"
                      if raisers else "")})
        actions.append({
            "verb": "revl_revoke", "arguments": {"hash": h},
            "why": "answer NO; the re-issue this ticket holds is refused once "
                   "and fires nothing"})
    elif state["state"] == STATE_GRANTED:
        actions.append({
            "verb": "re-issue the crossing",
            "arguments": {"component": ticket.get("component"),
                          "key": ticket.get("key"),
                          "method": ticket.get("method")},
            "why": "the minted yes is consumed by the identical call; the "
                   "candidate hash must still match"})
        actions.append({
            "verb": "revl_revoke", "arguments": {"hash": h},
            "why": "withdraw the minted but unspent yes"})
    elif state["state"] == STATE_REVOKED:
        actions.append({
            "verb": "re-issue the crossing for a fresh question",
            "arguments": {"component": ticket.get("component")},
            "why": "the revoked asking is refused; a new asking opens a new "
                   "round (after an E-Stop the session is dead and the way back "
                   "is `revl recover`)"})
    elif state["state"] == STATE_EXPIRED:
        actions.append({
            "verb": "re-issue the crossing for a fresh question",
            "arguments": {"component": ticket.get("component")},
            "why": "the question or its approval lapsed; a new asking opens a "
                   "new round"})
    elif state["state"] == STATE_CONSUMED:
        actions.append({
            "verb": "inspect the spend",
            "arguments": {"requestId": grants[0].get("requestId") if grants else None},
            "why": "the yes was spent before its crossing fired; the durable "
                   "`approval-consumed`/`approval-emission` join says whether "
                   "the crossing returned"})
    return {"model": None, "modelDetail": _MODEL_ADVICE_ABSENT,
            "actions": actions}


def _row(session, ticket: dict, now: int) -> dict:
    """One ticket, in the shape both `list` and `show` build on."""
    h = ticket.get("hash")
    round_ = _rounds(session).get(h, 1)
    state, _ = _state(session, ticket, now)
    quorum = _quorum_projection(session, ticket)
    entries = _entries_for(session, h)
    raisers = [name for name in _pending_raisers(session).get(h, {}) if name]
    row = {
        "hash": h,
        "effect": _effect(ticket),
        "candidateHash": ticket.get("candidateHash"),
        "argsDigest": ticket.get("argsDigest"),
        "round": round_,
        "state": state,
        "raisers": raisers,
        "proposers": sorted(_proposers(session, h)),
        "capabilities": list(ticket.get("capabilities") or []),
        "classCCapabilities": list(ticket.get("classCCapabilities") or []),
        "grants": len(entries),
        "pendingRaisers": raisers,
        "expiryRemainingMs": state.get("expiryRemainingMs"),
        "openedAt": state.get("openedAt"),
        "ageMs": (None if state.get("openedAt") is None
                  else max(0, now - int(state["openedAt"]))),
    }
    if state["state"] == STATE_PENDING and state.get("openedAt") is None:
        row["ageDetail"] = _SINGLE_PARTY_AGE_ABSENT
        row["expiryDetail"] = _SINGLE_PARTY_EXPIRY_ABSENT
    if quorum is not None:
        row["quorum"] = {"require": quorum.get("require"),
                         "counted": quorum.get("counted"),
                         "outcome": quorum.get("outcome"),
                         "expiresAt": quorum.get("expiresAt")}
    return row


def _proposers(session, ticket_hash: str) -> set:
    table = getattr(session, "_ticket_proposers", None)
    if isinstance(table, dict):
        return set(table.get(ticket_hash) or ())
    return set()


# ---------------------------------------------------------------------------
# the three verbs
# ---------------------------------------------------------------------------

def list_approvals(session, *, pending: bool = False,
                   session_id: str | None = None) -> dict:
    """Every ticket the live session has issued, newest asking round first.

    ``pending`` keeps only a question still waiting for an answer (a single-party
    asking with no grant, or a multi-party question open to votes and not past
    its deadline). ``session_id`` is accepted for a caller that names the live
    session explicitly; the surface reads THIS session, so a different id is a
    refusal rather than a silent whole-table read.
    """
    current = _session_id(session)
    if session_id is not None and current is not None and session_id != current:
        raise ValueError(
            f"this surface reads the live session `{current}`, not `{session_id}`: "
            f"the approvals are session-bound (item 246 invariant 5), so there is "
            f"nothing to list for another session id")
    now = _now(session)
    rows = [_row(session, ticket, now) for ticket in _tickets(session).values()]
    if pending:
        rows = [row for row in rows if row["state"]["state"] == STATE_PENDING]
    rows.sort(key=lambda row: (row.get("openedAt") or 0), reverse=True)
    states = {}
    for row in rows:
        key = row["state"]["state"]
        states[key] = states.get(key, 0) + 1
    return {"action": "list", "session": current,
            "pendingOnly": bool(pending), "count": len(rows),
            "countsByState": states, "tickets": rows,
            "detail": ("the live session's issued tickets; a pending "
                       "single-party question carries no ttl of its own, so its "
                       "expiry is absent rather than invented")}


def show_approval(session, ticket_hash: str, *, diff: bool = False) -> dict:
    """Everything one ticket records, and (with ``diff``) what a yes would let
    through: the crossing, its capability reach, resource bindings and secret
    disclosures, the policy rules that answer for it, the binding hashes, the
    decision graph, grant provenance and the next actions."""
    ticket = _tickets(session).get(ticket_hash)
    if ticket is None:
        raise ValueError(
            f"unknown ticket hash {ticket_hash!r}: the live session never issued "
            f"it, or the generation changed and the outstanding-ticket table was "
            f"replaced (item 246)")
    now = _now(session)
    row = _row(session, ticket, now)
    quorum = _quorum_projection(session, ticket)
    grants = _grants(session, ticket_hash, now)
    spends = _spends(session, [g.get("requestId") for g in grants])
    out = {
        "action": "show",
        "session": _session_id(session),
        "ticket": row,
        "body": dict(ticket),
        "policy": _policy(session, ticket),
        "bindings": _bindings(session, ticket, row["round"]),
        "grants": grants,
        "spends": spends,
        "refusal": _refusals(session).get(ticket_hash),
        "quorum": quorum,
        "advice": _advice(session, ticket, row["state"],
                          quorum=quorum, grants=grants),
    }
    if diff:
        out["diff"] = _blast_radius(ticket)
    return out


def _blast_radius(ticket: dict) -> dict:
    """What the ticket's crossing reaches, stated as the three things an approver
    is actually deciding about: external effects, secret flows and capability
    widenings - plus the crossings themselves, each with whether a checked
    inverse exists, so "emission or inverse" is never a question the reader has
    to infer."""
    crossings = []
    external = 0
    uninvertible = 0
    compensated = 0
    for crossing in ticket.get("crossings") or []:
        if not isinstance(crossing, dict):
            continue
        kind = crossing.get("kind")
        action_class = crossing.get("actionClass")
        if kind == "emission":
            text = f"emit {crossing.get('key')}.{crossing.get('method')}"
        elif kind == "extern":
            text = f"{crossing.get('class') or 'extern'} {crossing.get('name')}"
        elif kind == "widening":
            text = "first-class widening: an emitting callable escapes as a value"
        else:
            text = str(kind)
        item = {"kind": kind, "component": crossing.get("component"),
                "scope": crossing.get("scope"),
                "actionClass": action_class, "text": text,
                "compensated": bool(crossing.get("compensated")),
                "relay": bool(crossing.get("relay"))}
        if kind == "widening":
            item["capability"] = crossing.get("capability")
            item["note"] = crossing.get("note")
        if action_class == "c":
            external += 1
            if kind == "widening":
                item["classification"] = "no checked inverse: the reach cannot " \
                                        "be named, so it cannot be proven " \
                                        "reversible"
                uninvertible += 1
            elif crossing.get("compensated"):
                item["classification"] = "one-way emission with a declared " \
                                        "compensation"
                compensated += 1
            else:
                item["classification"] = "one-way emission with no checked inverse"
                uninvertible += 1
        elif action_class == "b":
            item["classification"] = "deferred emission, fired at commit"
        elif action_class == "a":
            item["classification"] = "witnessed crossing with a registered inverse"
        crossings.append(item)
    resource_scopes = dict(ticket.get("resourceScopes") or {})
    return {
        "effectClass": "c" if any(c.get("actionClass") == "c"
                                  for c in crossings) else None,
        "crossings": crossings,
        "externalEffects": external,
        "oneWayWithoutInverse": uninvertible,
        "compensatedEmissions": compensated,
        "secretFlows": {
            "taintOrigins": list(ticket.get("taintOrigins") or []),
            "untrustedArguments": list(ticket.get("untrustedArguments") or []),
            "resourceScopes": resource_scopes,
        },
        "capabilityWidenings": {
            "capabilities": list(ticket.get("capabilities") or []),
            "classCCapabilities": list(ticket.get("classCCapabilities") or []),
            "closureComponents": list(ticket.get("closureComponents") or []),
            "valueWidening": [c["text"] for c in crossings
                              if c.get("kind") == "widening"],
            "ceilings": ticket.get("ceilings"),
        },
        "resourceScopeRefusals": dict(ticket.get("resourceScopeRefusals") or {}),
        "disclosures": {
            "realm": ticket.get("realm"),
            "resourceScopesFromCallerArgs":
                list(ticket.get("resourceScopesFromCallerArgs") or []),
            "resourceScopeDurability": ticket.get("resourceScopeDurability"),
            "unreviewedHostCode": ticket.get("unreviewedHostCode"),
        },
    }


# ---------------------------------------------------------------------------
# stats: the in-memory mirror folded with the durable log
# ---------------------------------------------------------------------------

def _merge_records(session) -> tuple[list, dict]:
    """This session's in-memory ``_approval_records`` folded with the durable
    records ``_wal_ledger_records`` reads back. The durable log outlives the
    session and may share a path with earlier sessions, so it is the wider view;
    the in-memory mirror is used when the log is closed or unreadable, and rows
    the log does not carry are kept rather than dropped."""
    memory = _approval_records(session)
    try:
        durable = session._wal_ledger_records()
    except Exception:  # noqa: BLE001
        durable = []
    durable = durable if isinstance(durable, list) else []
    seen = set()
    merged = []
    for record in list(durable) + list(memory):
        if not isinstance(record, dict):
            continue
        key = (record.get("record"), record.get("requestId"),
               record.get("voteId"), record.get("at"),
               record.get("grantedAt"), record.get("hash"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(record)
    return merged, {"inMemory": len(memory), "durable": len(durable),
                    "merged": len(merged)}


def stats(session, *, session_id: str | None = None) -> dict:
    """Counts by state, the average time a question took to decide, and the
    override/escalate history, folded from the in-memory decision mirror and the
    durable approval log."""
    current = _session_id(session)
    if session_id is not None and current is not None and session_id != current:
        raise ValueError(
            f"this surface reads the live session `{current}`, not `{session_id}`: "
            f"the decision graph is session-bound (item 246 invariant 5)")
    records, sources = _merge_records(session)
    now = _now(session)
    counts: dict = {}
    decisions: dict = {}
    overrides = []
    escalations = []
    revoked = []
    expired = []
    opens: dict = {}
    terminal: dict = {}
    grants = 0
    consumed = 0
    revoked_grants = 0
    # A question closes through ONE terminal row, but the protocol writes more
    # than one on some paths (an override records `quorum-override` and then
    # `quorum-satisfied`; an escalation can be followed by a later override).
    # Fold by request id, last terminal wins, so a question is counted once.
    terminal_order = ("quorum-satisfied", "quorum-denied", "quorum-expired",
                      "quorum-escalated", "quorum-revoked", "quorum-override")
    for record in records:
        kind = record.get("record")
        if kind == "quorum-open":
            opens[record.get("requestId")] = record
        elif kind in terminal_order:
            terminal[record.get("requestId")] = record
        elif kind == "approval-granted":
            grants += 1
        elif kind == "approval-consumed":
            consumed += 1
        elif kind == "approval-revoked":
            revoked_grants += 1
        elif kind == "ticket-revoked":
            revoked.append({"hash": record.get("hash"),
                            "component": record.get("component"),
                            "by": record.get("by"),
                            "reason": record.get("reason"),
                            "estop": bool(record.get("estop")),
                            "at": record.get("at")})
    # the histories are per ROW, not per question: an escalation has its own row
    # even when a later override closed the question, and an override has one
    # even when `quorum-satisfied` follows it
    for record in records:
        kind = record.get("record")
        if kind == "quorum-override":
            overrides.append({"requestId": record.get("requestId"),
                              "hash": record.get("hash"),
                              "by": record.get("by"),
                              "reason": record.get("reason"),
                              "selfOverride": record.get("selfOverride"),
                              "at": record.get("at")})
        elif kind == "quorum-escalated":
            escalations.append({"requestId": record.get("requestId"),
                                "hash": record.get("hash"),
                                "by": record.get("by"),
                                "reason": record.get("reason"),
                                "at": record.get("resolvedAt")
                                or record.get("at")})
    for request_id, record in terminal.items():
        kind = record.get("record")
        resolved = record.get("resolvedAt") or record.get("at")
        if kind == "quorum-satisfied":
            counts[STATE_SATISFIED] = counts.get(STATE_SATISFIED, 0) + 1
            if record.get("satisfiedBy") == "override":
                counts["satisfiedByOverride"] = \
                    counts.get("satisfiedByOverride", 0) + 1
        elif kind == "quorum-denied":
            counts[STATE_DENIED] = counts.get(STATE_DENIED, 0) + 1
        elif kind == "quorum-expired":
            counts[STATE_EXPIRED] = counts.get(STATE_EXPIRED, 0) + 1
            expired.append({"requestId": request_id,
                            "hash": record.get("hash"), "at": resolved})
        elif kind == "quorum-escalated":
            counts[STATE_ESCALATED] = counts.get(STATE_ESCALATED, 0) + 1
        elif kind in ("quorum-revoked", "quorum-override"):
            # an override that did not reach `quorum-satisfied` (an interrupted
            # write) still counts as an override, never as a vote
            if kind == "quorum-override":
                counts["satisfiedByOverride"] = \
                    counts.get("satisfiedByOverride", 0) + 1
            else:
                counts[STATE_REVOKED] = counts.get(STATE_REVOKED, 0) + 1
                revoked.append({"requestId": request_id,
                                "hash": record.get("hash"),
                                "by": record.get("by"),
                                "reason": record.get("reason"),
                                "estop": bool(record.get("estop")),
                                "at": resolved})
        opened = opens.get(request_id)
        if opened is not None and resolved is not None \
                and opened.get("openedAt") is not None:
            decisions[request_id] = max(
                0, int(resolved) - int(opened["openedAt"]))
    counts[STATE_PENDING] = sum(
        1 for request_id in opens if request_id not in terminal)
    counts[STATE_GRANTED] = max(0, grants - consumed - revoked_grants)
    if consumed:
        counts[STATE_CONSUMED] = consumed
    if revoked_grants:
        counts[STATE_REVOKED] = counts.get(STATE_REVOKED, 0) + revoked_grants
    # the live table's own pending states, so a session with no WAL still counts
    live_states: dict = {}
    for ticket in _tickets(session).values():
        state, _ = _state(session, ticket, now)
        live_states[state["state"]] = live_states.get(state["state"], 0) + 1
    samples = sorted(decisions.values())
    average = (sum(samples) / len(samples)) if samples else None
    return {"action": "stats", "session": current,
            "counts": counts,
            "liveCounts": live_states,
            "averageTimeToDecisionMs": average,
            "decisionsMeasured": len(samples),
            "timesToDecisionMs": {key: decisions[key] for key in sorted(decisions)},
            "overrides": overrides,
            "escalations": escalations,
            "revoked": revoked,
            "expired": expired,
            "sources": sources,
            "detail": ("`averageTimeToDecisionMs` counts only questions whose "
                       "`quorum-open` row carries `openedAt`, so a single-party "
                       "grant contributes a count but no timing sample; the "
                       "in-memory mirror and the durable log are de-duplicated "
                       "before folding")}

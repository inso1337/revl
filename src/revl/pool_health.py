"""Liveness for a private-pool member: a signed probe, a signed heartbeat, and
a health record the operator reads (item 524, issue #1198).

What this closes
================

`revl pool status` showed what each member HOLDS and, once the dispatcher
existed, what it OWES. It did not show whether the member was still there. An
operator deciding whether to withdraw a peer had to find out by sending it
work and waiting for `peer-unreachable`, which writes an outstanding task into
the ledger as the price of the question.

This module answers the question without dispatching anything. The operator
sends a PROBE, the member answers with a HEARTBEAT, and the result is recorded
in `health.json` beside the roster and the ledger. `pool status` shows it on
each member's row, and `pool status --require-live SECONDS` exits nonzero when
a member has not been verified live inside that window, so a cron job or a
dashboard can watch the pool without holding any key.

A heartbeat is a claim, so it is verified before it is used
===========================================================

"Something answered at that address" is not "the member is alive". An address
is a hint; identity is a key. A heartbeat therefore counts only when it is
signed by an ACTIVE key the pool's directory pins for that member, and echoes
the nonce and the digest of the probe it answers. Each check fails closed and
names one link:

============================ =================================================
link                         what it means
============================ =================================================
``not-a-member``             the peer is not on the roster; there is nobody to
                             probe
``no-address``               no ``--peer-addr`` was given and no verified
                             contact ever recorded one
``peer-unreachable``         the channel failed. Recorded as ``unreachable``
``probe-refused``            something answered and refused the probe, with
                             its own link carried through. An unsigned refusal
                             proves nothing about who sent it, so it is
                             recorded as ``unverified``, never as alive
``heartbeat-shape``          the answer is not a heartbeat
``heartbeat-identity``       the heartbeat names another peer
``heartbeat-signature``      it does not verify under any key the directory
                             pins for this member
``heartbeat-key``            it verifies, under a key that confers no
                             authority now (superseded or revoked). A true
                             statement about a key, and not a live member
``stale-heartbeat``          it does not echo THIS probe's nonce and digest:
                             a captured heartbeat replayed by whatever now
                             answers at the address
``charter-identity``         it names another pool or charter
============================ =================================================

The peer side refuses a probe on the same terms it refuses a task: it must be
signed by the operator key the peer pinned (``probe-signature``), and name
this pool, this charter and this peer. That keeps the peer from signing
heartbeats for strangers, and keeps an unauthenticated party from learning
which identity answers at an address.

What liveness does NOT do
=========================

It confers no authority and removes none. A probe never writes the roster, the
charter, the identity directory or the ledger; a test walks this module's AST
and asserts it calls none of `admit`, `promote`, `withdraw`, `save_roster`,
`save_directory` or `save_ledger`. A member that stops answering is shown as
unreachable, with what it owes on the same row, and stays a member: removing it
is `revl pool withdraw`, which checks the charter's revoke authority and hands
its outstanding work to `lawful_retry`. A second path that withdrew a peer for
missing heartbeats would be a second, weaker authority beside the one the
charter declares, which is the failure item 524 exists to avoid.

The time recorded is the OPERATOR's clock at the moment the heartbeat verified.
The peer's own `answered_at` is kept for the record and never used to decide
anything, because it is a value the peer chose.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from . import peer_identity, peer_pool, pool_state

# ---------------------------------------------------------------------------
# kinds, domains, states, links
# ---------------------------------------------------------------------------

PROBE_KIND = "revl.pool-probe"
PROBE_VERSION = "1.0"
HEARTBEAT_KIND = "revl.pool-heartbeat"
HEARTBEAT_VERSION = "1.0"
HEALTH_KIND = "revl.pool-health"
HEALTH_VERSION = "1.0"
HEALTH_FILE = "health.json"

#: Two protocols, two domains, both distinct from the task, receipt, join and
#: charter domains. Without that a heartbeat signature would verify as some
#: other record's under the same key.
PROBE_DOMAIN = b"revl.pool-probe/v1\x00"
HEARTBEAT_DOMAIN = b"revl.pool-heartbeat/v1\x00"

#: A heartbeat under an active pinned key echoed this probe.
HEALTH_LIVE = "live"
#: Nothing answered.
HEALTH_UNREACHABLE = "unreachable"
#: Something answered, and what it said did not verify as this member.
HEALTH_UNVERIFIED = "unverified"
#: Never contacted. Not a failure: nobody has asked yet.
HEALTH_UNKNOWN = "unknown"

LINK_NOT_A_MEMBER = "not-a-member"
LINK_NO_ADDRESS = "no-address"
LINK_PEER_UNREACHABLE = "peer-unreachable"
LINK_PROBE_REFUSED = "probe-refused"
LINK_HEARTBEAT_SHAPE = "heartbeat-shape"
LINK_HEARTBEAT_IDENTITY = "heartbeat-identity"
LINK_HEARTBEAT_SIGNATURE = "heartbeat-signature"
LINK_HEARTBEAT_KEY = "heartbeat-key"
LINK_STALE_HEARTBEAT = "stale-heartbeat"
LINK_CHARTER_IDENTITY = "charter-identity"
# The peer side.
LINK_PROBE_SHAPE = "probe-shape"
LINK_PROBE_SIGNATURE = "probe-signature"
LINK_POOL_IDENTITY = "pool-identity"
LINK_PEER_IDENTITY = "peer-identity"

#: Every link this module can refuse on. A test asserts the set is exactly the
#: declared `LINK_` constants and that a run reaches every one of them.
REFUSAL_LINKS: tuple[str, ...] = (
    LINK_NOT_A_MEMBER,
    LINK_NO_ADDRESS,
    LINK_PEER_UNREACHABLE,
    LINK_PROBE_REFUSED,
    LINK_HEARTBEAT_SHAPE,
    LINK_HEARTBEAT_IDENTITY,
    LINK_HEARTBEAT_SIGNATURE,
    LINK_HEARTBEAT_KEY,
    LINK_STALE_HEARTBEAT,
    LINK_CHARTER_IDENTITY,
    LINK_PROBE_SHAPE,
    LINK_PROBE_SIGNATURE,
    LINK_POOL_IDENTITY,
    LINK_PEER_IDENTITY,
)

_PROBE_FIELDS = ("pool_id", "charter_digest", "peer_id", "nonce", "issued_at")
_HEARTBEAT_FIELDS = ("pool_id", "charter_digest", "peer_id", "nonce",
                     "probe_digest", "answered_at")


class HealthError(ValueError):
    """A caller-side fault: an unreadable health file, or one bound to another
    charter. A PEER-supplied record never raises; it gets a refusal."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _refusal(link: str, reason: str, **extra: Any) -> dict:
    return {"ok": False, "link": link, "reason": reason, **extra}


def _missing_string(record: Mapping[str, Any], names) -> str:
    for name in names:
        value = record.get(name)
        if not isinstance(value, str) or not value:
            return name
    return ""


# ---------------------------------------------------------------------------
# the probe (operator -> peer)
# ---------------------------------------------------------------------------


def build_probe(*, pool_id: str, charter_digest: str, peer_id: str,
                nonce: str = "", at: Optional[datetime] = None) -> dict:
    """The probe BODY. The nonce is what makes a heartbeat fresh: the operator
    accepts only a heartbeat that echoes the nonce it just sent."""
    return {"kind": PROBE_KIND, "version": PROBE_VERSION,
            "pool_id": pool_id, "charter_digest": charter_digest,
            "peer_id": peer_id, "nonce": nonce or secrets.token_hex(16),
            "issued_at": _iso(at or _utc_now())}


def sign_probe(body: Mapping[str, Any],
               identity: peer_identity.PeerIdentity, *,
               network_exposed: bool = False) -> dict:
    """Signed under the operator's key pair, the same key a task is signed
    with, so the peer checks both against the one operator key it pinned.
    ``network_exposed`` is passed to :func:`revl.peer_identity.sign_record`
    (issue #1460)."""
    return peer_identity.sign_record(PROBE_DOMAIN, body, identity,
                                     network_exposed=network_exposed)


def probe_digest(probe: Mapping[str, Any]) -> str:
    return peer_pool.canonical_digest(probe)


def is_probe(record: Any) -> bool:
    """Does this frame claim to be a probe? Only the claim: the checks are in
    `answer_probe`. The peer's runner uses this to route a frame."""
    return isinstance(record, Mapping) and record.get("kind") == PROBE_KIND


def _probe_shape(record: Any) -> str:
    if not isinstance(record, Mapping):
        return "the record is not an object"
    if record.get("kind") != PROBE_KIND:
        return f"kind is {record.get('kind')!r}, expected {PROBE_KIND!r}"
    missing = _missing_string(record, _PROBE_FIELDS)
    return f"{missing} is missing or not a string" if missing else ""


def _probe_addressing(record: Mapping[str, Any], *, pool_id: str,
                      charter_digest: str, peer_id: str) -> Optional[dict]:
    """Is this probe for this pool, this charter and this peer?"""
    if record["pool_id"] != pool_id:
        return _refusal(LINK_POOL_IDENTITY,
                        f"the probe names pool {record['pool_id']!r} and this "
                        f"peer joined {pool_id!r}")
    if record["charter_digest"] != charter_digest:
        return _refusal(LINK_CHARTER_IDENTITY,
                        f"the probe pins charter "
                        f"{record['charter_digest'][:16]} and this peer holds "
                        f"{charter_digest[:16]}")
    if record["peer_id"] != peer_id:
        return _refusal(LINK_PEER_IDENTITY,
                        f"the probe is addressed to {record['peer_id']!r} and "
                        f"this peer is {peer_id!r}")
    return None


def answer_probe(record: Any, *, charter_record: Mapping[str, Any],
                 identity: peer_identity.PeerIdentity,
                 operator_public: peer_identity.PublicIdentity,
                 at: Optional[datetime] = None,
                 network_exposed: bool = False) -> dict:
    """The PEER side: check a probe and, if it passes, sign a heartbeat.

    Never raises: the wire is hostile. The heartbeat carries the probe's nonce
    and digest, so it answers exactly one probe and cannot be reused for
    another. ``network_exposed`` is passed to
    :func:`revl.peer_identity.sign_record` (issue #1460); the runner that
    serves off loopback has already refused to bind without the backend."""
    shape = _probe_shape(record)
    if shape:
        return _refusal(LINK_PROBE_SHAPE, f"not a probe: {shape}")
    ok, reason = peer_identity.verify_record(PROBE_DOMAIN, record,
                                             operator_public.public_key)
    if not ok:
        return _refusal(LINK_PROBE_SIGNATURE,
                        f"the probe is not signed by the pinned operator "
                        f"key: {reason}")
    wrong = _probe_addressing(
        record, pool_id=str(charter_record.get("pool_id", "")),
        charter_digest=peer_pool.canonical_digest(charter_record),
        peer_id=identity.peer_id)
    if wrong is not None:
        return wrong
    body = {"kind": HEARTBEAT_KIND, "version": HEARTBEAT_VERSION,
            "pool_id": record["pool_id"],
            "charter_digest": record["charter_digest"],
            "peer_id": identity.peer_id, "nonce": record["nonce"],
            "probe_digest": probe_digest(record),
            "answered_at": _iso(at or _utc_now())}
    return {"ok": True,
            "heartbeat": peer_identity.sign_record(
                HEARTBEAT_DOMAIN, body, identity,
                network_exposed=network_exposed)}


# ---------------------------------------------------------------------------
# the heartbeat (peer -> operator)
# ---------------------------------------------------------------------------


def _heartbeat_shape(record: Any) -> str:
    if not isinstance(record, Mapping):
        return "the answer carries no heartbeat object"
    if record.get("kind") != HEARTBEAT_KIND:
        return f"kind is {record.get('kind')!r}, expected {HEARTBEAT_KIND!r}"
    missing = _missing_string(record, _HEARTBEAT_FIELDS)
    return f"{missing} is missing or not a string" if missing else ""


def _heartbeat_authority(heartbeat: Mapping[str, Any], peer_id: str,
                         directory: peer_identity.IdentityDirectory,
                         when: Optional[datetime]) -> Optional[dict]:
    """Signed by a key pinned for this member, and is that key active now?
    Two separate findings: a forgery is not a revoked key."""
    attribution = directory.attribute(HEARTBEAT_DOMAIN, heartbeat, peer_id,
                                      when=when)
    if not attribution.verified:
        return _refusal(LINK_HEARTBEAT_SIGNATURE,
                        f"the heartbeat does not verify under any key pinned "
                        f"for {peer_id!r}: {attribution.reason}")
    if not attribution.confers_authority:
        return _refusal(LINK_HEARTBEAT_KEY,
                        f"the heartbeat verifies under {attribution.key_id}, "
                        f"which confers no authority now: "
                        f"{attribution.reason}")
    return None


def check_heartbeat(heartbeat: Any, *, probe: Mapping[str, Any],
                    member: peer_pool.Membership,
                    charter_record: Mapping[str, Any],
                    directory: peer_identity.IdentityDirectory,
                    when: Optional[datetime] = None) -> dict:
    """The OPERATOR side: is this the member, answering THIS probe?

    Identity is checked before the signature so the refusal names the right
    fault, and freshness after it, so a replayed heartbeat is reported as
    stale only once it is known to be genuinely the member's."""
    shape = _heartbeat_shape(heartbeat)
    if shape:
        return _refusal(LINK_HEARTBEAT_SHAPE, shape)
    if heartbeat["peer_id"] != member.peer_id:
        return _refusal(LINK_HEARTBEAT_IDENTITY,
                        f"the heartbeat names {heartbeat['peer_id']!r} and "
                        f"the probe went to {member.peer_id!r}")
    refused = _heartbeat_authority(heartbeat, member.peer_id, directory, when)
    if refused is not None:
        return refused
    if heartbeat["nonce"] != probe["nonce"] \
            or heartbeat["probe_digest"] != probe_digest(probe):
        return _refusal(LINK_STALE_HEARTBEAT,
                        "the heartbeat answers a different probe; a captured "
                        "heartbeat replayed at the address is not a live "
                        "member")
    if heartbeat["pool_id"] != charter_record.get("pool_id") \
            or heartbeat["charter_digest"] \
            != peer_pool.canonical_digest(charter_record):
        return _refusal(LINK_CHARTER_IDENTITY,
                        f"the heartbeat names charter "
                        f"{heartbeat['charter_digest'][:16]} of pool "
                        f"{heartbeat['pool_id']!r}, not this pool's")
    return {"ok": True, "key_id": str(heartbeat.get("key_id", "")),
            "answered_at": heartbeat["answered_at"]}


# ---------------------------------------------------------------------------
# the health record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Contact:
    """One observation of one member, as the operator made it."""

    peer_id: str
    state: str
    at: str
    source: str
    addr: str = ""
    key_id: str = ""
    link: str = ""
    reason: str = ""


class HealthLog:
    """Per-member liveness, plus an append-only event list.

    The per-member row is the CURRENT reading; the events are the history, so
    a member that flapped and a member that was always up do not render the
    same. `last_live` survives failures: an unreachable member still shows
    when it was last verified, which is the fact an operator weighs against
    what it owes."""

    def __init__(self, pool_id: str, charter_digest: str):
        self.pool_id = pool_id
        self.charter_digest = charter_digest
        self.members: dict[str, dict] = {}
        self.events: list[dict] = []

    def record(self, contact: Contact) -> dict:
        row = dict(self.members.get(contact.peer_id) or {})
        failed = contact.state != HEALTH_LIVE
        was_failing = row.get("state") not in (None, HEALTH_LIVE)
        row.update({"state": contact.state, "checked_at": contact.at,
                    "source": contact.source, "link": contact.link,
                    "reason": contact.reason})
        if failed:
            row["failures"] = int(row.get("failures", 0)) + 1
            row["since"] = row.get("since") if was_failing else contact.at
        else:
            row.update({"failures": 0, "since": "", "last_live": contact.at,
                        "addr": contact.addr, "key_id": contact.key_id})
        row.setdefault("last_live", "")
        row.setdefault("addr", "")
        self.members[contact.peer_id] = row
        self.events.append({k: v for k, v in vars(contact).items() if v})
        return row

    def row(self, peer_id: str) -> dict:
        return dict(self.members.get(peer_id) or {"state": HEALTH_UNKNOWN})

    def as_dict(self) -> dict:
        return {"kind": HEALTH_KIND, "version": HEALTH_VERSION,
                "pool_id": self.pool_id,
                "charter_digest": self.charter_digest,
                "members": {k: dict(v) for k, v in sorted(self.members.items())},
                "events": list(self.events)}

    @classmethod
    def from_dict(cls, payload: Any) -> "HealthLog":
        if not isinstance(payload, Mapping):
            raise HealthError("a health record must be an object")
        log = cls(str(payload.get("pool_id", "")),
                  str(payload.get("charter_digest", "")))
        members = payload.get("members") or {}
        if isinstance(members, Mapping):
            log.members = {str(k): dict(v) for k, v in members.items()
                           if isinstance(v, Mapping)}
        events = payload.get("events") or []
        if isinstance(events, list):
            log.events = [dict(e) for e in events if isinstance(e, Mapping)]
        return log


def load_health(pool_dir) -> HealthLog:
    """The pool's health record, or an empty one bound to its charter. Bound
    like the ledger: a re-chartered pool starts a new record rather than
    inheriting observations made under other terms."""
    charter_record, _ = peer_pool.load_pool(pool_dir)
    digest = peer_pool.canonical_digest(charter_record)
    path = Path(pool_dir) / HEALTH_FILE
    if not path.exists():
        return HealthLog(str(charter_record.get("pool_id", "")), digest)
    try:
        log = HealthLog.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        raise HealthError(f"{path}: {error}") from error
    if log.charter_digest != digest:
        raise HealthError(
            f"{path} was written against charter {log.charter_digest[:16]} "
            f"and this pool's charter is {digest[:16]}; a re-chartered pool "
            f"starts a new health record rather than inheriting one")
    return log


def save_health(pool_dir, log: HealthLog) -> None:
    """Atomic. A caller that read the record to change it holds
    `pool_state.locked` from that read to this write."""
    pool_state.write_json(Path(pool_dir) / HEALTH_FILE, log.as_dict())


def note_contact(pool_dir, contact: Contact) -> None:
    """Record one observation made elsewhere (a dispatch). Used by
    `pool_dispatch`, so a delivery verified under the member's key counts as a
    live contact and a dispatch that found nobody counts as unreachable."""
    with pool_state.locked(pool_dir):
        log = load_health(pool_dir)
        log.record(contact)
        save_health(pool_dir, log)


# ---------------------------------------------------------------------------
# reading it
# ---------------------------------------------------------------------------


def _age(stamp: str, now: datetime) -> Optional[float]:
    moment = peer_pool._parse_iso(stamp) if stamp else None
    return None if moment is None else (now - moment).total_seconds()


def is_live(row: Mapping[str, Any], *, within: float,
            now: Optional[datetime] = None) -> bool:
    """Verified live, most recently, no longer than `within` seconds ago. The
    CURRENT state must be live too: a member that answered an hour ago and
    failed a probe a minute ago is not live."""
    if row.get("state") != HEALTH_LIVE:
        return False
    age = _age(str(row.get("last_live", "")), now or _utc_now())
    return age is not None and 0 <= age <= within


def not_live(log: HealthLog, peer_ids, *, within: float,
             now: Optional[datetime] = None) -> list[str]:
    """The members, of those named, that `is_live` does not hold for."""
    return sorted(peer for peer in peer_ids
                  if not is_live(log.row(peer), within=within, now=now))


def health_note(row: Mapping[str, Any]) -> str:
    """The fragment `pool status` puts on a member's row."""
    state = row.get("state", HEALTH_UNKNOWN)
    if state == HEALTH_UNKNOWN:
        return " health=unknown"
    if state == HEALTH_LIVE:
        return f" health=live@{row.get('last_live', '')}"
    note = (f" health={state} since={row.get('since', '')} "
            f"failures={row.get('failures', 0)}")
    if row.get("link"):
        note += f" link={row['link']}"
    return note + f" last-live={row.get('last_live') or 'never'}"


# ---------------------------------------------------------------------------
# the operator side: one probe, end to end
# ---------------------------------------------------------------------------


def _record(log: HealthLog, peer_id: str, state: str, at: datetime, *,
            addr: str, link: str = "", reason: str = "",
            key_id: str = "") -> dict:
    return log.record(Contact(peer_id=peer_id, state=state, at=_iso(at),
                              source="probe", addr=addr, key_id=key_id,
                              link=link, reason=reason))


def _settle(outcome: dict, log: HealthLog, peer_id: str, addr: str,
            at: datetime) -> dict:
    """Write what the probe learned. An unanswered probe is `unreachable`; any
    other failure means something answered and did not prove it was the
    member, which is `unverified`."""
    if outcome.get("ok"):
        row = _record(log, peer_id, HEALTH_LIVE, at, addr=addr,
                      key_id=outcome.get("key_id", ""))
    else:
        link = str(outcome.get("link", ""))
        state = HEALTH_UNREACHABLE if link == LINK_PEER_UNREACHABLE \
            else HEALTH_UNVERIFIED
        row = _record(log, peer_id, state, at, addr=addr, link=link,
                      reason=str(outcome.get("reason", "")))
    return dict(outcome, peer_id=peer_id, addr=addr, health=row)


def _exchange(probe: Mapping[str, Any], *, host: str, port: int,
              timeout: float) -> dict:
    from .pool_dispatch import LINK_PEER_UNREACHABLE as UNREACHABLE  # noqa: PLC0415
    from .pool_dispatch import send_task  # noqa: PLC0415 (lazy: no cycle)

    answer = send_task(probe, host=host, port=port, timeout=timeout)
    if answer.get("ok"):
        return answer
    if answer.get("link") == UNREACHABLE:
        return _refusal(LINK_PEER_UNREACHABLE, str(answer.get("reason", "")))
    return _refusal(LINK_PROBE_REFUSED,
                    f"the peer refused the probe on "
                    f"{answer.get('link', '?')}: {answer.get('reason', '')}",
                    peer_link=str(answer.get("link", "")))


def probe_member(*, pool_dir, peer_id: str, addr: str,
                 dispatch_identity: peer_identity.PeerIdentity,
                 timeout: float = 10.0,
                 when: Optional[datetime] = None) -> dict:
    """Probe one member and record the result. `addr` is HOST:PORT, or "" to
    use the address of the member's last verified contact."""
    from .pool_dispatch import _split_addr, signer_exposure  # noqa: PLC0415

    charter_record, roster = peer_pool.load_pool(pool_dir)
    member = roster.members.get(peer_id)
    if member is None:
        return _refusal(LINK_NOT_A_MEMBER,
                        f"{peer_id!r} is not a member of pool "
                        f"{charter_record.get('pool_id')!r}", peer_id=peer_id)
    addr = addr or str(load_health(pool_dir).row(peer_id).get("addr", ""))
    if not addr:
        return _refusal(LINK_NO_ADDRESS,
                        f"no address is known for {peer_id!r}; pass "
                        f"--peer-addr once, and a verified answer records it",
                        peer_id=peer_id)
    host, port = _split_addr(addr)
    # Before the probe is signed, sent or recorded: off loopback this needs
    # the constant-time signing backend, and refuses naming it (issue #1460).
    exposed = signer_exposure(host, f"a probe to the remote peer {host}")
    probe = sign_probe(build_probe(
        pool_id=str(charter_record.get("pool_id", "")),
        charter_digest=peer_pool.canonical_digest(charter_record),
        peer_id=peer_id, at=when), dispatch_identity,
        network_exposed=exposed)
    answer = _exchange(probe, host=host, port=port, timeout=timeout)
    outcome = answer if not answer.get("ok") else check_heartbeat(
        answer.get("heartbeat"), probe=probe, member=member,
        charter_record=charter_record,
        directory=peer_pool.load_directory(pool_dir), when=when)
    # The record is read again under the lock, not carried from before the
    # exchange: another probe or a dispatch may have written a row for some
    # other member while this one was waiting, and that row must survive.
    with pool_state.locked(pool_dir):
        log = load_health(pool_dir)
        settled = _settle(outcome, log, peer_id, addr, when or _utc_now())
        save_health(pool_dir, log)
    return settled


# ---------------------------------------------------------------------------
# the CLI: `revl pool probe`
# ---------------------------------------------------------------------------


def _targets(args, roster: peer_pool.Roster) -> list[str]:
    return list(args.peer or ()) or sorted(roster.members)


def _print_outcome(outcome: Mapping[str, Any]) -> None:
    peer = outcome.get("peer_id", "")
    if outcome.get("ok"):
        print(f"{peer}  live  key={outcome.get('key_id', '')} "
              f"addr={outcome.get('addr', '')}")
        return
    state = (outcome.get("health") or {}).get("state", "not-recorded")
    print(f"{peer}  {state}  link={outcome.get('link', '')}: "
          f"{outcome.get('reason', '')}")


def probe_command(args) -> int:
    """`revl pool probe`: ask each named member (default: every member) to
    prove it is there, record the answer in `health.json`, and exit 1 if any
    did not. Changes no authority, so it needs only the key that signs a
    probe, which is the key that signs a task."""
    import sys  # noqa: PLC0415

    from .pool_dispatch import DispatchError  # noqa: PLC0415

    charter_record, roster = peer_pool.load_pool(args.dir)
    targets = _targets(args, roster)
    if not targets:
        # Nothing probed is not everything live. Say so and fail, so a health
        # check pointed at an empty pool does not read as green.
        print(f"pool {charter_record.get('pool_id', '')} has no members to "
              f"probe", file=sys.stderr)
        return 1
    if args.peer_addr and len(args.peer or ()) != 1:
        print("error: --peer-addr names one member's address; give exactly "
              "one --peer with it", file=sys.stderr)
        return 2
    try:
        identity = peer_identity.load_private_identity(args.dispatch_identity)
        outcomes = [probe_member(pool_dir=args.dir, peer_id=peer,
                                 addr=args.peer_addr or "",
                                 dispatch_identity=identity,
                                 timeout=args.timeout)
                    for peer in targets]
    except (OSError, ValueError, HealthError, DispatchError,
            peer_identity.IdentityError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps({"pool_id": charter_record.get("pool_id", ""),
                          "probes": outcomes}, indent=2, sort_keys=True))
    else:
        for outcome in outcomes:
            _print_outcome(outcome)
    return 0 if all(o.get("ok") for o in outcomes) else 1

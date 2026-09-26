"""Dispatching work to a private-pool member, and the ledger that says it was
delivered once (item 524, issue #1198).

What this closes
================

`peer_pool` stands a pool up and admits a peer to it. `pool_receipt` checks
what a peer signed about work it ran. Between those two there was nothing: no
way to hand an admitted member a unit of work, no channel to hand it over, and
no ledger recording that the result came back exactly once. So
`Roster.outstanding` was a shape with no populator, a withdrawal reported an
empty `orphaned` set on every real deployment, and `revl run --pool private`
did not exist.

This module is that middle. It is deliberately small, because every decision it
needs was already made somewhere else and copying one here would be the
"second, weaker policy engine" item 524 warns about:

* what a member may be SENT is `peer_pool.work_admissible`, keyed on
  `lawful_retry.EffectClass`;
* whether a result is usable is `pool_receipt.check_receipt`, which is
  signatures, key authority and binding;
* whether the bytes in hand are the bytes a receipt is about is
  `pool_receipt.verify_result`, which is a hash;
* what a lost peer's outstanding work becomes is
  `lawful_retry.dispatch_on_loss`.

What is genuinely new here is three things: a signed task envelope, a delivery
ledger with one-result semantics, and a loopback channel over which the two
ends actually talk.

The task envelope, and which way each check fails
================================================

A task is signed by the OPERATOR under an identity key pair, in its own domain
(`revl.pool-task/v1`), and the peer verifies it under a public key it pinned
out of band. That is the same shape as the peer's own identity: a key, not an
address. The peer holds no operator secret, which is the point -- the peer is
the machine nobody trusts, so nothing it holds may be sufficient to admit
anyone, including itself.

Every check on the peer side fails CLOSED, and each names one link:

============================ =================================================
link                         what it means
============================ =================================================
``task-shape``               the record is not a task; refused before a member
                             of it is read as meaningful
``task-signature``           it is not signed by the pinned operator key, or
                             it was edited after signing
``pool-identity``            it names another pool
``charter-identity``         it names another charter of this pool. A charter
                             re-signed with different terms is a different
                             charter, so a task minted under the old ones stops
                             verifying
``peer-identity``            it is addressed to another peer. A task relayed to
                             a second peer is not a second admission
``artifact-digest``          the bytes do not hash to the digest the task
                             pins, or that digest is not one the charter
                             admits. The peer RE-HASHES what it is about to
                             run and never trusts the declared value
``unknown-runner``           the runner is not one this peer implements.
                             Refused rather than defaulted, so adding a runner
                             on one side and not the other leaves it
                             inadmissible everywhere
``replayed-task``            this peer already ran that task id. One result per
                             task on the peer side too, so a captured task
                             cannot be re-run for a second receipt
============================ =================================================

The operator side adds four more, on the way back:

============================ =================================================
link                         what it means
============================ =================================================
``peer-unreachable``         the channel failed. NOT a refusal of the peer: the
                             task stays ``dispatched`` in the ledger and is
                             outstanding, because a task whose fate is unknown
                             must not be recorded as either done or dropped
``result-digest``            the result in hand does not hash to what the
                             receipt pins
``receipt-refused``          `pool_receipt.check_receipt` refused, and its own
                             link is carried through rather than flattened
``delivered-twice``          the ledger already holds a delivered result for
                             this task. Refused AND recorded: a delivered-twice
                             attempt is visible, never silently absorbed
============================ =================================================

Why the effect class is DERIVED and not declared
================================================

A tier is a bound on what a member may be sent, expressed in
`lawful_retry.EffectClass`. If the operator could state a task's class on the
command line, the bound would be self-declared and the ladder would mean
nothing. So it is computed from the composition's own audited boundary, and
the computation is deliberately the most conservative one that is defensible:

**a composition is dispatchable exactly when its G8 boundary is empty** -- no
externs, no emissions, no capabilities, no compensations, no awaits, no
capability registers and no recovery surface. That is `EffectClass.PURE` and
nothing else is claimed. Anything with a boundary is refused on
``effect-class-unproven``, naming what was seen.

That is a real limit and it is stated rather than papered over: an extern whose
declared *class* is ``pure`` still runs host code (`stdlib`'s file writer is
one), so reading a declared class as an effect class would be fail-OPEN on
exactly the arrow that sends work to a machine nobody trusts. Classifying a
composition WITH a boundary is the work that unlocks the ``replayable`` and
``durable`` tiers, and it is not done here. Until it is, those tiers admit
nothing through this dispatcher, which is the honest direction: a tier with no
classifier reachable to it grants no authority by accident.

The channel, and what it does not give you
==========================================

Newline-delimited JSON over TCP, bound to loopback by default. A non-loopback
bind needs ``--allow-remote`` and prints what it costs, because the channel
carries no transport security: every record on it is SIGNED, so nothing on the
wire can be forged or altered undetected, but nothing on the wire is secret
either. The artifact source crosses it in the clear. Cross-machine dispatch
over a confidential channel is roadmap item 118's mTLS work
(`revl deploy`, issue #79) and this module is deliberately its caller rather
than a second implementation of it.
"""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from . import peer_identity, peer_pool, pool_receipt
from .lawful_retry import EffectClass

# ---------------------------------------------------------------------------
# kinds, domains and the runner set
# ---------------------------------------------------------------------------

TASK_KIND = "revl.pool-task"
TASK_VERSION = "1.0"

#: Distinct from every other domain in the pool protocols, for the reason
#: `peer_pool` and `pool_receipt` each give: without separation one protocol's
#: signature verifies as another's under the same key.
TASK_DOMAIN = b"revl.pool-task/v1\x00"

RESULT_KIND = "revl.pool-result"
RESULT_VERSION = "1.0"

LEDGER_KIND = "revl.pool-delivery-ledger"
LEDGER_VERSION = "1.0"
LEDGER_FILE = "ledger.json"

#: Run the artifact's own declared tests on the peer. Needs nothing but the
#: revl frontend, so it is the runner that works on any machine that has revl.
RUNNER_TEST_PY = "test-py"
#: Boot the composition, prove teardown leaves no residue, exit. Needs a
#: cordis-py runtime on the PEER, which is the peer's business and not the
#: operator's: a peer without one refuses the task with the runtime's own
#: message rather than pretending to have run it.
RUNNER_RUN_ONCE_PY = "run-once-py"

#: An unlisted runner is refused, never defaulted. A runner added on one side
#: and not the other is then inadmissible everywhere, which is the fail-closed
#: direction.
RUNNERS: dict[str, tuple[str, ...]] = {
    RUNNER_TEST_PY: ("test", "{artifact}", "--backend", "py"),
    RUNNER_RUN_ONCE_PY: ("run", "--once", "{artifact}"),
}

#: The one pool kind `run --pool` accepts. A public or swarm pool is not a
#: missing value here: it needs a different threat model (issue #1198's own
#: "alternatives considered"), so it is absent rather than unimplemented.
POOL_PRIVATE = "private"

#: The most a peer may send back. A peer is untrusted, so the operator bounds
#: the read rather than letting the far side choose how much memory it takes.
MAX_FRAME_BYTES = 8 * 1024 * 1024

# ---------------------------------------------------------------------------
# refusal links
# ---------------------------------------------------------------------------
# Lowercase links, following `peer_pool`, `pool_receipt` and `revl.deploy`.
# Nothing here enters the guarantee register in `revl.diagnostics`: a dispatch
# refusal is a decision about a deployment, not a verdict about a program, and
# there is no program to write under `examples/rejections/` for "this peer
# already ran that task id". A test asserts this module never names that
# register, which is why it is described here rather than spelled.

LINK_TASK_SHAPE = "task-shape"
LINK_TASK_SIGNATURE = "task-signature"
LINK_POOL_IDENTITY = "pool-identity"
LINK_CHARTER_IDENTITY = "charter-identity"
LINK_PEER_IDENTITY = "peer-identity"
LINK_ARTIFACT_DIGEST = "artifact-digest"
LINK_UNKNOWN_RUNNER = "unknown-runner"
LINK_REPLAYED_TASK = "replayed-task"
LINK_NOT_A_MEMBER = "not-a-member"
LINK_EFFECT_CLASS_UNPROVEN = "effect-class-unproven"
LINK_WORK_INADMISSIBLE = "work-inadmissible"
LINK_PEER_UNREACHABLE = "peer-unreachable"
LINK_RESULT_DIGEST = "result-digest"
LINK_RECEIPT_REFUSED = "receipt-refused"
LINK_DELIVERED_TWICE = "delivered-twice"
LINK_LATE_DELIVERY = "late-delivery"
LINK_UNKNOWN_TASK = "unknown-task"
LINK_NON_LOOPBACK_BIND = "non-loopback-bind"
LINK_MULTI_FILE_ARTIFACT = "multi-file-artifact"
LINK_UNSUPPORTED_WITH_POOL = "unsupported-with-pool"

#: Every link this module can refuse on. A test asserts the set is exactly the
#: links the code reaches, in both directions, so a link cannot be declared and
#: left unreachable -- which reads as a gate that exists -- nor reached and left
#: unnamed.
REFUSAL_LINKS: tuple[str, ...] = (
    LINK_TASK_SHAPE,
    LINK_TASK_SIGNATURE,
    LINK_POOL_IDENTITY,
    LINK_CHARTER_IDENTITY,
    LINK_PEER_IDENTITY,
    LINK_ARTIFACT_DIGEST,
    LINK_UNKNOWN_RUNNER,
    LINK_REPLAYED_TASK,
    LINK_NOT_A_MEMBER,
    LINK_EFFECT_CLASS_UNPROVEN,
    LINK_WORK_INADMISSIBLE,
    LINK_PEER_UNREACHABLE,
    LINK_RESULT_DIGEST,
    LINK_RECEIPT_REFUSED,
    LINK_DELIVERED_TWICE,
    LINK_LATE_DELIVERY,
    LINK_UNKNOWN_TASK,
    LINK_NON_LOOPBACK_BIND,
    LINK_MULTI_FILE_ARTIFACT,
    LINK_UNSUPPORTED_WITH_POOL,
)

#: `revl run` flags the LOCAL runner honours and a pool dispatch cannot, with
#: the value that means "not given". A flag that is accepted and ignored is not
#: implemented, so `--pool private` refuses any of these by name rather than
#: taking the work somewhere the flag does not reach. `--backend` is here at its
#: default because the runner is chosen with `--pool-runner`, which is the
#: peer's tier and not the operator's.
LOCAL_ONLY_RUN_FLAGS: tuple[tuple[str, str, Any], ...] = (
    ("backend", "--backend", "py"),
    ("config", "--config", None),
    ("env", "--env", None),
    ("policy", "--policy", None),
    ("watch", "--watch", False),
    ("record", "--record", False),
    ("estop_latch", "--estop-latch", None),
    ("wal", "--wal", None),
    ("trace", "--trace", None),
    ("withdraw", "--withdraw", None),
    ("plan", "--plan", False),
    ("placement", "--placement", None),
    ("once", "--once", False),
)

# ---------------------------------------------------------------------------
# ledger states
# ---------------------------------------------------------------------------

#: Sent, fate unknown. The one state a withdrawal turns into `orphaned`.
STATE_DISPATCHED = "dispatched"
#: A result came back, hashed to what its receipt pins, and the receipt was
#: checked. Terminal: a second delivery is refused.
STATE_DELIVERED = "delivered"
#: The peer left while this was outstanding. Terminal here; what to DO about it
#: is `lawful_retry.dispatch_on_loss`, which this module records and does not
#: re-decide.
STATE_ORPHANED = "orphaned"
#: A result came back and did not pass. Terminal, and kept: a refusal that is
#: deleted is a refusal nobody can audit.
STATE_REFUSED = "refused"


class DispatchError(ValueError):
    """A caller-side fault: a malformed pool directory, an unusable key. A
    PEER-supplied record never raises; it gets a refusal, because a record that
    can choose a traceback over a refusal has chosen its own outcome."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _refusal(link: str, reason: str, **extra: Any) -> dict:
    """One shape for every refusal: a named link, a reason a human can act on,
    and whatever context the caller can use. Never an exception."""
    return {"ok": False, "link": link, "reason": reason, **extra}


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------


def artifact_digest(source: bytes) -> str:
    """The digest an artifact is pinned by: sha256 over the source bytes.

    Over the BYTES, not over a parsed form, so there is nothing between what
    was hashed and what will be read. Both ends compute it the same way and the
    peer computes it on what it received rather than reading what the task
    claims."""
    return hashlib.sha256(bytes(source)).hexdigest()


@dataclass(frozen=True)
class BoundarySurface:
    """What the audit found at a composition's G8 boundary, counted.

    Carried rather than reduced to a boolean so a refusal can name what it
    saw: "3 externs, 1 emission" is actionable and "not pure" is not."""

    externs: int = 0
    emissions: int = 0
    capabilities: int = 0
    compensated: int = 0
    awaits: int = 0
    capability_registers: int = 0
    recovery_surface: int = 0

    @property
    def empty(self) -> bool:
        return not any((self.externs, self.emissions, self.capabilities,
                        self.compensated, self.awaits,
                        self.capability_registers, self.recovery_surface))

    def as_dict(self) -> dict:
        return {"externs": self.externs, "emissions": self.emissions,
                "capabilities": self.capabilities,
                "compensated": self.compensated, "awaits": self.awaits,
                "capability_registers": self.capability_registers,
                "recovery_surface": self.recovery_surface}

    def describe(self) -> str:
        named = [f"{count} {name.replace('_', ' ')}"
                 for name, count in sorted(self.as_dict().items()) if count]
        return ", ".join(named) if named else "nothing"


def boundary_surface(ir: Mapping[str, Any]) -> BoundarySurface:
    """Count the composition's boundary from the audit report `revl audit`
    prints, so this reads the SAME surface an operator can inspect by hand
    rather than a private walk that could disagree with it."""
    from .audit_diff import audit_report  # noqa: PLC0415 (lazy: pulls the walk)

    report = audit_report(dict(ir))
    boundary = report.get("boundary") or {}
    emissions = capabilities = compensated = awaits = 0
    for entry in boundary.values():
        if not isinstance(entry, Mapping):
            continue
        emissions += len(entry.get("emissions") or ())
        capabilities += len(entry.get("capabilities") or ())
        compensated += int(entry.get("compensated") or 0)
        awaits += int(entry.get("awaits") or 0)
    return BoundarySurface(
        externs=len(report.get("externs") or ()),
        emissions=emissions,
        capabilities=capabilities,
        compensated=compensated,
        awaits=awaits,
        capability_registers=len(report.get("capability_registers") or ()),
        recovery_surface=len(report.get("recovery_surface") or ()))


def classify_artifact(ir: Mapping[str, Any]
                      ) -> tuple[Optional[EffectClass], str, BoundarySurface]:
    """The effect class of a composition, or ``None`` with the reason it could
    not be proved.

    Only one answer is ever returned, and it is the one that needs no trust: a
    composition whose audited boundary is EMPTY crosses nothing, so it is
    `EffectClass.PURE`. A composition with a boundary is unproven here, not
    "probably fine": see this module's docstring for why reading an extern's
    declared ``class`` would be fail-open."""
    surface = boundary_surface(ir)
    if surface.empty:
        return (EffectClass.PURE,
                "the audited G8 boundary is empty: the composition crosses "
                "nothing, so it is pure", surface)
    return (None,
            f"the composition has a G8 boundary ({surface.describe()}) and "
            f"this dispatcher classifies only boundary-free compositions; a "
            f"composition that crosses is unproven here, not assumed safe",
            surface)


# ---------------------------------------------------------------------------
# the task envelope
# ---------------------------------------------------------------------------


def build_task(*, pool_id: str, charter_digest: str, peer_id: str,
               task_id: str, artifact: bytes, runner: str,
               effect_class: EffectClass,
               at: Optional[datetime] = None) -> dict:
    """The task BODY, before it is signed.

    ``artifact_digest`` is computed here from the bytes rather than accepted as
    a parameter, so the operator cannot sign a task whose declared digest is
    not the digest of the artifact it carries."""
    source = bytes(artifact)
    return {
        "kind": TASK_KIND,
        "version": TASK_VERSION,
        "pool_id": pool_id,
        "charter_digest": charter_digest,
        "peer_id": peer_id,
        "task_id": task_id,
        "artifact_digest": artifact_digest(source),
        "artifact": source.decode("utf-8"),
        "runner": runner,
        "effect_class": effect_class.value,
        "issued_at": _iso(at or _utc_now()),
    }


def sign_task(body: Mapping[str, Any],
              identity: peer_identity.PeerIdentity) -> dict:
    """Sign a task under the operator's identity key pair.

    Asymmetric, so the peer holds only a public key. A peer that could verify a
    task under a SHARED key could also mint one, and a peer that can mint its
    own work has no bound at all."""
    if not isinstance(identity, peer_identity.PeerIdentity):
        raise DispatchError("signing a task needs the operator's PeerIdentity")
    return peer_identity.sign_record(TASK_DOMAIN, body, identity)


def _task_shape(record: Any) -> str:
    """Why this is not a task, or "" if it is one. Checked before any member is
    read as meaningful, so a hostile record is refused on shape and not on
    whatever the first field access happened to raise."""
    if not isinstance(record, Mapping):
        return "the record is not an object"
    if record.get("kind") != TASK_KIND:
        return f"kind is {record.get('kind')!r}, expected {TASK_KIND!r}"
    for name in ("pool_id", "charter_digest", "peer_id", "task_id",
                 "artifact_digest", "artifact", "runner", "effect_class",
                 "issued_at"):
        value = record.get(name)
        if not isinstance(value, str) or (not value and name != "artifact"):
            return f"{name} is missing or not a string"
    # The task id NAMES A DIRECTORY on the peer, so it is constrained here
    # rather than sanitised at the place it is joined onto a path. A signature
    # proves who sent a task; it does not make `../../etc` a safe file name,
    # and the peer is the side that would pay for the difference.
    task_id = record["task_id"]
    if len(task_id) > 64 or not all(
            c.isascii() and (c.isalnum() or c in "-_.") for c in task_id) \
            or task_id in (".", ".."):
        return ("task_id must be 1-64 characters of ASCII letters, digits, "
                "'-', '_' or '.', because it names a directory on the peer")
    return ""


def verify_task(record: Any, operator_public: peer_identity.PublicIdentity
                ) -> tuple[bool, str]:
    """Is this task signed by the operator key this peer pinned?

    The pinned key is the verifier's, never the record's: `verify_record`
    compares the embedded public key against the pinned one and refuses a
    mismatch rather than verifying under whatever the record carries."""
    shape = _task_shape(record)
    if shape:
        return False, shape
    if not isinstance(operator_public, peer_identity.PublicIdentity):
        return False, "no pinned operator identity to check the task against"
    return peer_identity.verify_record(TASK_DOMAIN, record,
                                       operator_public.public_key)


# ---------------------------------------------------------------------------
# the delivery ledger
# ---------------------------------------------------------------------------


class DeliveryLedger:
    """One-result delivery, recorded.

    Append-only, for the reason `peer_pool`'s roster is: a task that was
    delivered twice and a task that was delivered once must not render the
    same, so a refused second delivery is APPENDED as an event rather than
    dropped on the floor. "Impossible or visible" is item 524's wording and
    this is the visible half; the impossible half is that the state machine has
    no edge from `delivered` back to `dispatched`.

    The ledger is a JSON file with no concurrency control, the same limit
    `peer_pool`'s roster carries and for the same assumed deployment: one
    operator writing."""

    def __init__(self, pool_id: str, charter_digest: str):
        self.pool_id = pool_id
        self.charter_digest = charter_digest
        #: task_id -> the task's record. One entry per task, ever.
        self.tasks: dict[str, dict] = {}
        #: Every state change, in order, including the refused ones.
        self.events: list[dict] = []

    # -- reads ------------------------------------------------------------

    def outstanding(self) -> dict[str, list[str]]:
        """peer_id -> the task ids still `dispatched`. This is what
        `Roster.outstanding` wanted a populator for, and what a withdrawal
        reads to name its `orphaned` set."""
        out: dict[str, list[str]] = {}
        for task_id, entry in sorted(self.tasks.items()):
            if entry.get("state") == STATE_DISPATCHED:
                out.setdefault(entry.get("peer_id", ""), []).append(task_id)
        return out

    def counts(self) -> dict[str, int]:
        tally = {STATE_DISPATCHED: 0, STATE_DELIVERED: 0,
                 STATE_ORPHANED: 0, STATE_REFUSED: 0}
        for entry in self.tasks.values():
            state = entry.get("state", "")
            if state in tally:
                tally[state] += 1
        return tally

    # -- writes -----------------------------------------------------------

    def _event(self, event: str, task_id: str, **extra: Any) -> dict:
        record = {"event": event, "task_id": task_id,
                  "at": _iso(_utc_now()), **extra}
        self.events.append(record)
        return record

    def dispatch(self, *, task_id: str, peer_id: str, artifact_digest: str,
                 runner: str, effect_class: str) -> dict:
        """Record that a task went out. Refuses a task id that was used
        before, so a captured task id cannot open a second slot to deliver
        into."""
        if task_id in self.tasks:
            prior = self.tasks[task_id]
            self._event("dispatch-refused", task_id, link=LINK_REPLAYED_TASK,
                        state=prior.get("state", ""))
            return _refusal(
                LINK_REPLAYED_TASK,
                f"task {task_id!r} is already in the ledger, state "
                f"{prior.get('state', '')!r}; a task id is dispatched once",
                task_id=task_id)
        self.tasks[task_id] = {
            "task_id": task_id, "peer_id": peer_id,
            "artifact_digest": artifact_digest, "runner": runner,
            "effect_class": effect_class, "state": STATE_DISPATCHED,
            "dispatched_at": _iso(_utc_now()),
        }
        self._event("dispatched", task_id, peer_id=peer_id,
                    artifact_digest=artifact_digest)
        return {"ok": True, "task_id": task_id, "state": STATE_DISPATCHED}

    def deliver(self, *, task_id: str, peer_id: str, result_digest: str,
                receipt_digest: str, counts: bool,
                receipt: Optional[Mapping[str, Any]] = None,
                attestation: Optional[Mapping[str, Any]] = None) -> dict:
        """Record the one result.

        Three refusals, all of them recorded rather than dropped: an unknown
        task, a task already delivered, and a task whose peer left while it was
        outstanding. The last one is separate from the second on purpose: they
        are different findings about what the pool did, and an operator
        reconciling a ledger needs to tell them apart."""
        entry = self.tasks.get(task_id)
        if entry is None:
            self._event("delivery-refused", task_id, link=LINK_UNKNOWN_TASK)
            return _refusal(LINK_UNKNOWN_TASK,
                            f"no task {task_id!r} was dispatched from this "
                            f"ledger; a result for work nobody sent is not a "
                            f"delivery", task_id=task_id)
        state = entry.get("state", "")
        if state == STATE_DELIVERED:
            self._event("delivery-refused", task_id,
                        link=LINK_DELIVERED_TWICE,
                        result_digest=result_digest,
                        first_result_digest=entry.get("result_digest", ""))
            return _refusal(
                LINK_DELIVERED_TWICE,
                f"task {task_id!r} was already delivered at "
                f"{entry.get('delivered_at', '')}; the second result is "
                f"refused and recorded, not absorbed", task_id=task_id)
        if state == STATE_ORPHANED:
            self._event("delivery-refused", task_id, link=LINK_LATE_DELIVERY,
                        result_digest=result_digest)
            return _refusal(
                LINK_LATE_DELIVERY,
                f"task {task_id!r} was orphaned when peer "
                f"{entry.get('peer_id', '')!r} was withdrawn; a result "
                f"arriving after that is recorded and does not revive the "
                f"task", task_id=task_id)
        entry.update({"state": STATE_DELIVERED,
                      "result_digest": result_digest,
                      "receipt_digest": receipt_digest,
                      "counts_as_evidence": bool(counts),
                      "delivered_at": _iso(_utc_now())})
        # The signed pair itself, not just its digest. `peer_pool.promote`
        # RECOUNTS evidence from `(receipt, attestation)` pairs and takes no
        # count from a caller, so the ledger has to hold the records or the
        # evidence a delivery produced would not survive the process that saw
        # it. Both are signed, so keeping them here cannot forge anything.
        if isinstance(receipt, Mapping):
            entry["receipt"] = dict(receipt)
        if isinstance(attestation, Mapping):
            entry["attestation"] = dict(attestation)
        self._event("delivered", task_id, peer_id=peer_id,
                    result_digest=result_digest,
                    receipt_digest=receipt_digest, counts=bool(counts))
        return {"ok": True, "task_id": task_id, "state": STATE_DELIVERED}

    def refuse(self, *, task_id: str, link: str, reason: str) -> dict:
        """A result came back and did not pass. The task is terminal and the
        refusal is kept: a refusal that is deleted is a refusal nobody can
        audit."""
        entry = self.tasks.get(task_id)
        if entry is not None and entry.get("state") == STATE_DISPATCHED:
            entry.update({"state": STATE_REFUSED, "link": link,
                          "reason": reason,
                          "refused_at": _iso(_utc_now())})
        self._event("refused", task_id, link=link, reason=reason)
        return {"ok": True, "task_id": task_id, "state": STATE_REFUSED}

    def orphan(self, peer_id: str, *, reason: str) -> list[str]:
        """A peer left. Its outstanding tasks become `orphaned` and are named.

        What to DO about each is `lawful_retry.dispatch_on_loss`; this records
        that they are owed, which is the fact the withdrawal receipt reports
        and which was previously always the empty set."""
        moved = []
        for task_id, entry in sorted(self.tasks.items()):
            if entry.get("peer_id") == peer_id \
                    and entry.get("state") == STATE_DISPATCHED:
                entry.update({"state": STATE_ORPHANED, "reason": reason,
                              "orphaned_at": _iso(_utc_now())})
                self._event("orphaned", task_id, peer_id=peer_id,
                            reason=reason)
                moved.append(task_id)
        return moved

    # -- persistence ------------------------------------------------------

    def as_dict(self) -> dict:
        return {"kind": LEDGER_KIND, "version": LEDGER_VERSION,
                "pool_id": self.pool_id,
                "charter_digest": self.charter_digest,
                "tasks": {k: dict(v) for k, v in sorted(self.tasks.items())},
                "events": list(self.events)}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DeliveryLedger":
        if not isinstance(payload, Mapping):
            raise DispatchError("a delivery ledger must be an object")
        ledger = cls(str(payload.get("pool_id", "")),
                     str(payload.get("charter_digest", "")))
        tasks = payload.get("tasks") or {}
        if isinstance(tasks, Mapping):
            ledger.tasks = {str(k): dict(v) for k, v in tasks.items()
                            if isinstance(v, Mapping)}
        events = payload.get("events") or []
        if isinstance(events, list):
            ledger.events = [dict(e) for e in events if isinstance(e, Mapping)]
        return ledger


def load_ledger(pool_dir) -> DeliveryLedger:
    """The pool's ledger, or an empty one bound to the pool's own charter.

    A ledger belongs to a charter digest, so a pool re-chartered under new
    terms starts a new ledger rather than inheriting one whose tasks were
    admitted under the old ones."""
    charter_record, _ = peer_pool.load_pool(pool_dir)
    digest = peer_pool.canonical_digest(charter_record)
    path = Path(pool_dir) / LEDGER_FILE
    if not path.exists():
        return DeliveryLedger(str(charter_record.get("pool_id", "")), digest)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DispatchError(f"{path}: {error}") from error
    ledger = DeliveryLedger.from_dict(payload)
    if ledger.charter_digest != digest:
        raise DispatchError(
            f"{path} was written against charter {ledger.charter_digest[:16]} "
            f"and this pool's charter is {digest[:16]}; a re-chartered pool "
            f"starts a new ledger rather than inheriting one")
    return ledger


def save_ledger(pool_dir, ledger: DeliveryLedger) -> None:
    path = Path(pool_dir) / LEDGER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger.as_dict(), indent=2, sort_keys=True)
                    + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# the peer side: running one task
# ---------------------------------------------------------------------------


def _scrub(text: str, workspace: Path) -> list[str]:
    """Output lines with the peer's own workspace path replaced.

    A receipt is a signed, shared record, so a local absolute path in it is
    both a reproducibility problem -- the digest would differ per run for the
    same work -- and an unnecessary disclosure about the peer's machine."""
    replaced = text.replace(str(workspace), "<workspace>")
    return [line for line in replaced.splitlines()]


def execute(runner: str, source: bytes, workspace: Path,
            *, timeout: float = 300.0) -> dict:
    """Run one artifact in one workspace and return the result record.

    The artifact is written under ``workspace`` and named RELATIVELY on the
    command line, with ``workspace`` as the working directory, so no absolute
    path reaches the process's output and the result is the same wherever the
    workspace happens to be."""
    if runner not in RUNNERS:
        raise DispatchError(f"unknown runner {runner!r}")
    workspace.mkdir(parents=True, exist_ok=True)
    artifact = workspace / "artifact.rvl"
    artifact.write_bytes(bytes(source))
    # `-P` is issue #317's safety bit and it is load-bearing HERE rather than
    # merely conventional: the working directory is a scratch directory the
    # artifact is written into, so without it that directory would be on
    # `sys.path` for the interpreter that runs the artifact.
    argv = [sys.executable, "-P", "-m", "revl"] + [
        part.format(artifact=artifact.name) for part in RUNNERS[runner]]
    try:
        done = subprocess.run(argv, cwd=str(workspace), capture_output=True,
                              text=True, timeout=timeout, check=False)
        code, out, err = done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired:
        code, out, err = 124, "", f"the runner exceeded {timeout:g}s"
    except OSError as error:  # pragma: no cover - the interpreter is gone
        code, out, err = 125, "", f"the runner could not start: {error}"
    return {"kind": RESULT_KIND, "version": RESULT_VERSION, "runner": runner,
            "exit_code": code, "stdout": _scrub(out, workspace),
            "stderr": _scrub(err, workspace)}


class PeerRunner:
    """The peer side of the channel, with no socket in it.

    Split from the transport so every refusal above can be tested by handing
    this a record, which is also what keeps the hostile-input corpus honest: a
    test does not have to open a port to send a malformed task."""

    def __init__(self, *, charter_record: Mapping[str, Any],
                 identity: peer_identity.PeerIdentity,
                 operator_public: peer_identity.PublicIdentity,
                 workspace: Path, timeout: float = 300.0):
        self.charter_record = dict(charter_record)
        self.charter_digest = peer_pool.canonical_digest(self.charter_record)
        self.identity = identity
        self.operator_public = operator_public
        self.workspace = Path(workspace)
        self.timeout = timeout
        #: Task ids this peer has already run. One result per task on the peer
        #: side too, so a captured task cannot be re-run for a second receipt.
        self.seen: dict[str, str] = {}

    def handle(self, record: Any) -> dict:
        """Check one task and, if it passes every check, run it and sign a
        receipt. Never raises: the wire is hostile."""
        shape = _task_shape(record)
        if shape:
            return _refusal(LINK_TASK_SHAPE, f"not a task: {shape}")
        ok, reason = verify_task(record, self.operator_public)
        if not ok:
            return _refusal(LINK_TASK_SIGNATURE,
                            f"the task is not signed by the pinned operator "
                            f"key: {reason}",
                            task_id=record.get("task_id", ""))
        task_id = record["task_id"]
        if record["pool_id"] != self.charter_record.get("pool_id"):
            return _refusal(LINK_POOL_IDENTITY,
                            f"the task names pool {record['pool_id']!r} and "
                            f"this peer joined "
                            f"{self.charter_record.get('pool_id')!r}",
                            task_id=task_id)
        if record["charter_digest"] != self.charter_digest:
            return _refusal(
                LINK_CHARTER_IDENTITY,
                f"the task pins charter {record['charter_digest'][:16]} and "
                f"this peer holds {self.charter_digest[:16]}; a charter "
                f"re-signed with different terms is a different charter",
                task_id=task_id)
        if record["peer_id"] != self.identity.peer_id:
            return _refusal(LINK_PEER_IDENTITY,
                            f"the task is addressed to "
                            f"{record['peer_id']!r} and this peer is "
                            f"{self.identity.peer_id!r}", task_id=task_id)
        source = record["artifact"].encode("utf-8")
        actual = artifact_digest(source)
        if actual != record["artifact_digest"]:
            return _refusal(
                LINK_ARTIFACT_DIGEST,
                f"the artifact hashes to {actual[:16]} and the task pins "
                f"{record['artifact_digest'][:16]}; the peer runs what it "
                f"hashed, never what the task declared", task_id=task_id)
        admitted = self.charter_record.get("artifact_digests") or ()
        if actual not in admitted:
            return _refusal(
                LINK_ARTIFACT_DIGEST,
                f"artifact {actual[:16]} is not one the charter admits; the "
                f"peer refuses work the pool never declared",
                task_id=task_id)
        runner = record["runner"]
        if runner not in RUNNERS:
            return _refusal(
                LINK_UNKNOWN_RUNNER,
                f"runner {runner!r} is not implemented by this peer; "
                f"it runs {', '.join(sorted(RUNNERS))}", task_id=task_id)
        if task_id in self.seen:
            return _refusal(
                LINK_REPLAYED_TASK,
                f"this peer already ran task {task_id!r}; a task is run once, "
                f"so a captured task cannot be replayed for a second receipt",
                task_id=task_id)
        result = execute(runner, source, self.workspace / task_id,
                         timeout=self.timeout)
        receipt = pool_receipt.issue_receipt(
            pool_id=record["pool_id"], task_id=task_id,
            artifact_digest=actual, result=result, identity=self.identity)
        self.seen[task_id] = pool_receipt.receipt_digest(receipt)
        return {"ok": True, "task_id": task_id, "receipt": receipt,
                "result": result}


# ---------------------------------------------------------------------------
# the channel
# ---------------------------------------------------------------------------


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _read_frame(conn: socket.socket, limit: int = MAX_FRAME_BYTES) -> bytes:
    """One newline-terminated frame, bounded. A far side that never sends a
    newline cannot make the near side grow without limit."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if b"\n" in chunk:
            break
        if total > limit:
            raise DispatchError(f"frame exceeded {limit} bytes with no newline")
    return b"".join(chunks).split(b"\n", 1)[0]


def serve(runner: PeerRunner, *, host: str = "127.0.0.1", port: int = 0,
          allow_remote: bool = False, once: bool = False,
          on_listen=None, on_event=None) -> int:
    """Accept tasks on a socket and answer each with a receipt or a refusal.

    Loopback by default. A non-loopback bind is refused unless the operator
    asked for it, because the channel has no transport security: the records on
    it are signed and therefore unforgeable, and they are not secret."""
    if host not in LOOPBACK_HOSTS and not allow_remote:
        raise DispatchError(
            f"refusing to bind {host}: this channel is plaintext, so the "
            f"artifact source crosses it in the clear. Pass --allow-remote if "
            f"that is acceptable on your network, or keep it on loopback and "
            f"tunnel it ({LINK_NON_LOOPBACK_BIND})")
    served = 0
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET,
                       socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(8)
        bound = server.getsockname()
        if on_listen is not None:
            on_listen(bound[0], bound[1])
        while True:
            conn, _peer = server.accept()
            with conn:
                # A connection that says nothing must not hold the worker.
                # This loop serves one task at a time, so an unauthenticated
                # party that connects and never writes would otherwise take
                # the peer off the pool with no packets at all.
                conn.settimeout(runner.timeout)
                try:
                    frame = _read_frame(conn)
                    response = runner.handle(json.loads(frame))
                except (DispatchError, ValueError) as error:
                    response = _refusal(LINK_TASK_SHAPE,
                                        f"unreadable frame: {error}")
                except Exception as error:   # noqa: BLE001 - see below
                    # The frame is parsed BEFORE any signature is checked, so
                    # an unauthenticated party reaches this code. A worker that
                    # a stranger can kill is a worker a stranger can take off
                    # the pool, so the loop survives anything one frame can do
                    # and answers with a refusal. `handle` is written never to
                    # raise and the corpus pins that; this is the backstop for
                    # what the corpus has not thought of, not a substitute.
                    response = _refusal(
                        LINK_TASK_SHAPE,
                        f"the frame could not be processed: "
                        f"{type(error).__name__}")
                try:
                    conn.sendall(json.dumps(response, sort_keys=True).encode()
                                 + b"\n")
                except OSError as error:
                    # The far side went away between its request and its
                    # answer. Nothing to do about it and nothing to stop for:
                    # the work either ran or was refused, and both are already
                    # recorded on this side.
                    response = _refusal(
                        LINK_PEER_UNREACHABLE,
                        f"the answer could not be sent back: {error}",
                        task_id=response.get("task_id", ""))
            served += 1
            if on_event is not None:
                on_event(response)
            if once:
                break
    return served


def send_task(task_record: Mapping[str, Any], *, host: str, port: int,
              timeout: float = 300.0) -> dict:
    """Hand one signed task to a peer and read its answer.

    An unreachable peer is NOT a refusal of the peer: it is
    `peer-unreachable`, and the caller leaves the task outstanding, because a
    task whose fate is unknown must not be recorded as either done or
    dropped."""
    payload = json.dumps(dict(task_record), sort_keys=True).encode() + b"\n"
    try:
        with socket.create_connection((host, port), timeout=timeout) as conn:
            conn.sendall(payload)
            conn.shutdown(socket.SHUT_WR)
            frame = _read_frame(conn)
    except (OSError, DispatchError) as error:
        return _refusal(LINK_PEER_UNREACHABLE,
                        f"peer at {host}:{port} did not answer: {error}",
                        task_id=task_record.get("task_id", ""))
    try:
        answer = json.loads(frame)
    except ValueError as error:
        return _refusal(LINK_PEER_UNREACHABLE,
                        f"peer at {host}:{port} answered with something that "
                        f"is not JSON: {error}",
                        task_id=task_record.get("task_id", ""))
    if not isinstance(answer, Mapping):
        return _refusal(LINK_PEER_UNREACHABLE,
                        f"peer at {host}:{port} answered with "
                        f"{type(answer).__name__}, not an object",
                        task_id=task_record.get("task_id", ""))
    return dict(answer)


# ---------------------------------------------------------------------------
# the operator side: one dispatch, end to end
# ---------------------------------------------------------------------------


def verify_delivery(answer: Mapping[str, Any], *, charter_record: Mapping[str, Any],
                    member: peer_pool.Membership,
                    directory: peer_identity.IdentityDirectory,
                    attesting_identity: peer_identity.PeerIdentity,
                    when: Optional[datetime] = None) -> dict:
    """Check what came back: the hash first, then the receipt.

    In that order deliberately. A hash failure says the bytes in hand are not
    the bytes the receipt is about, and a receipt failure says the receipt is
    not the peer's; reporting the second when the first is true would name the
    wrong fault.

    The attestation is produced HERE, by the operator's attesting key, over the
    digest of the receipt the peer signed. `pool_receipt.check_receipt` then
    decides whether the pair counts; nothing in this module re-decides it."""
    receipt = answer.get("receipt")
    result = answer.get("result")
    if not isinstance(receipt, Mapping):
        return _refusal(LINK_RECEIPT_REFUSED,
                        "the peer returned no receipt")
    ok, reason = pool_receipt.verify_result(receipt, result)
    if not ok:
        return _refusal(LINK_RESULT_DIGEST, reason,
                        task_id=str(receipt.get("task_id", "")))
    attestation = pool_receipt.attest_receipt(receipt,
                                              identity=attesting_identity)
    check = pool_receipt.check_receipt(
        receipt, attestation, pool_id=str(charter_record.get("pool_id", "")),
        peer_id=member.peer_id, artifact_digest=member.artifact_digest,
        admitted_at=member.admitted_at,
        attest_key_ids=charter_record.get("attest_key_ids") or (),
        directory=directory, when=when)
    if check.link:
        return _refusal(LINK_RECEIPT_REFUSED,
                        f"the receipt was refused on {check.link}: "
                        f"{check.reason}",
                        task_id=check.task_id, receipt_link=check.link,
                        check=check.as_dict())
    return {"ok": True, "task_id": check.task_id,
            "receipt_digest": check.digest,
            "result_digest": str(receipt.get("result_digest", "")),
            "counts": check.counts, "check": check.as_dict(),
            "attestation": attestation}


def dispatch_one(*, pool_dir, peer_id: str, host: str, port: int,
                 source: bytes, runner: str,
                 dispatch_identity: peer_identity.PeerIdentity,
                 attesting_identity: peer_identity.PeerIdentity,
                 task_id: str = "", timeout: float = 300.0) -> dict:
    """One unit of work, from the operator's pool directory to a verified
    result and back into the ledger.

    The order is the whole point and it is the fail-closed one: classify,
    check the tier admits that class, WRITE THE DISPATCH DOWN, send, verify.
    The ledger entry is written before the task leaves, so a peer that takes
    the work and never answers leaves an outstanding task rather than no
    record at all -- which is the case a withdrawal has to be able to see."""
    from .compiler import compile_files  # noqa: PLC0415 (lazy)

    charter_record, roster = peer_pool.load_pool(pool_dir)
    directory = peer_pool.load_directory(pool_dir)
    ledger = load_ledger(pool_dir)

    member = roster.members.get(peer_id)
    if member is None:
        return _refusal(LINK_NOT_A_MEMBER,
                        f"{peer_id!r} is not a member of pool "
                        f"{charter_record.get('pool_id')!r}; a task is "
                        f"dispatched to an admitted peer or not at all")
    digest = artifact_digest(source)
    if digest != member.artifact_digest:
        return _refusal(
            LINK_ARTIFACT_DIGEST,
            f"the artifact hashes to {digest[:16]} and {peer_id!r} was "
            f"admitted for {member.artifact_digest[:16]}; the candidate a "
            f"peer runs is the candidate it was admitted with")
    if digest not in (charter_record.get("artifact_digests") or ()):
        return _refusal(LINK_ARTIFACT_DIGEST,
                        f"artifact {digest[:16]} is not one the charter "
                        f"admits")
    if runner not in RUNNERS:
        return _refusal(LINK_UNKNOWN_RUNNER,
                        f"runner {runner!r} is not one of "
                        f"{', '.join(sorted(RUNNERS))}")

    ir = compile_files(["artifact.rvl"],
                       sources={"artifact.rvl": source.decode("utf-8")})
    effect_class, reason, surface = classify_artifact(ir)
    if effect_class is None:
        return _refusal(LINK_EFFECT_CLASS_UNPROVEN, reason,
                        surface=surface.as_dict())
    admissible, why = peer_pool.work_admissible(member, effect_class)
    if not admissible:
        return _refusal(LINK_WORK_INADMISSIBLE, why,
                        effect_class=effect_class.value, tier=member.tier)

    import secrets  # noqa: PLC0415

    task_id = task_id or secrets.token_hex(8)
    written = ledger.dispatch(task_id=task_id, peer_id=peer_id,
                              artifact_digest=digest, runner=runner,
                              effect_class=effect_class.value)
    if not written.get("ok"):
        return written
    roster.outstanding = ledger.outstanding()
    peer_pool.save_roster(pool_dir, roster)
    save_ledger(pool_dir, ledger)

    body = build_task(pool_id=str(charter_record.get("pool_id", "")),
                      charter_digest=peer_pool.canonical_digest(charter_record),
                      peer_id=peer_id, task_id=task_id, artifact=source,
                      runner=runner, effect_class=effect_class)
    task_record = sign_task(body, dispatch_identity)
    answer = send_task(task_record, host=host, port=port, timeout=timeout)

    if not answer.get("ok"):
        link = str(answer.get("link", LINK_RECEIPT_REFUSED))
        if link != LINK_PEER_UNREACHABLE:
            # The peer answered and refused. That is a settled outcome, so the
            # task is terminal. An unreachable peer is NOT settled and stays
            # outstanding, which is the distinction a withdrawal reads.
            ledger.refuse(task_id=task_id, link=link,
                          reason=str(answer.get("reason", "")))
            roster.outstanding = ledger.outstanding()
            peer_pool.save_roster(pool_dir, roster)
        save_ledger(pool_dir, ledger)
        return dict(answer, task_id=task_id)

    verdict = verify_delivery(answer, charter_record=charter_record,
                              member=member, directory=directory,
                              attesting_identity=attesting_identity)
    if not verdict.get("ok"):
        ledger.refuse(task_id=task_id, link=str(verdict.get("link", "")),
                      reason=str(verdict.get("reason", "")))
        roster.outstanding = ledger.outstanding()
        peer_pool.save_roster(pool_dir, roster)
        save_ledger(pool_dir, ledger)
        return dict(verdict, task_id=task_id)

    delivered = ledger.deliver(task_id=task_id, peer_id=peer_id,
                               result_digest=verdict["result_digest"],
                               receipt_digest=verdict["receipt_digest"],
                               counts=verdict["counts"],
                               receipt=answer.get("receipt"),
                               attestation=verdict["attestation"])
    roster.outstanding = ledger.outstanding()
    peer_pool.save_roster(pool_dir, roster)
    save_ledger(pool_dir, ledger)
    if not delivered.get("ok"):
        return dict(delivered, task_id=task_id)
    return {"ok": True, "kind": "revl.pool-delivery", "version": "1.0",
            "pool_id": str(charter_record.get("pool_id", "")),
            "peer_id": peer_id, "task_id": task_id,
            "artifact_digest": digest, "runner": runner,
            "effect_class": effect_class.value, "tier": member.tier,
            "boundary": surface.as_dict(),
            "result_digest": verdict["result_digest"],
            "receipt_digest": verdict["receipt_digest"],
            "counts_as_evidence": verdict["counts"],
            "result": answer.get("result"),
            "receipt": answer.get("receipt"),
            "attestation": verdict["attestation"]}


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------


def _split_addr(value: str) -> tuple[str, int]:
    host, _, port = str(value).rpartition(":")
    if not host or not port.isdigit():
        raise DispatchError(f"--peer-addr {value!r} is not HOST:PORT")
    return host, int(port)


def serve_command(args) -> int:
    """`revl pool serve`: the PEER side, listening.

    It holds its own private identity and the operator's PUBLIC one. There is
    no secret here that could admit anybody, which is the property that makes
    running this on a machine the operator does not trust coherent."""
    charter_record = json.loads(Path(args.charter).read_text(encoding="utf-8"))
    identity = peer_identity.load_private_identity(args.identity_key)
    operator_public = peer_identity.load_public_identity(args.operator_public)
    workspace = Path(args.workdir) if args.workdir else None
    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="revl-pool-peer-") as scratch:
        runner = PeerRunner(charter_record=charter_record, identity=identity,
                            operator_public=operator_public,
                            workspace=workspace or Path(scratch),
                            timeout=args.timeout)

        def announce(host, port):
            print(f"peer {identity.peer_id} listening on {host}:{port} "
                  f"for pool {charter_record.get('pool_id')!r} "
                  f"charter {peer_pool.canonical_digest(charter_record)[:16]}",
                  flush=True)

        def report(response):
            if response.get("ok"):
                print(f"ran task {response.get('task_id', '')} -> receipt "
                      f"{pool_receipt.receipt_digest(response['receipt'])[:16]}",
                      flush=True)
            else:
                print(f"refused task {response.get('task_id', '')} on "
                      f"{response.get('link', '')}: "
                      f"{response.get('reason', '')}", flush=True)

        try:
            serve(runner, host=args.host, port=args.port,
                  allow_remote=args.allow_remote, once=args.once,
                  on_listen=announce, on_event=report)
        except DispatchError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:  # pragma: no cover - operator ^C
            return 0
    return 0


def ledger_command(args) -> int:
    """`revl pool ledger`: the delivery ledger, as an operator reads it.

    Needs no key, like `pool status` and for the same reason: reading what was
    delivered must not require touching the secret that admits."""
    ledger = load_ledger(args.dir)
    if getattr(args, "json", False):
        print(json.dumps(ledger.as_dict(), indent=2, sort_keys=True))
        return 0
    counts = ledger.counts()
    print(f"pool {ledger.pool_id}\n"
          f"  charter   {ledger.charter_digest[:16]}\n"
          f"  tasks     {len(ledger.tasks)} "
          f"({counts[STATE_DELIVERED]} delivered, "
          f"{counts[STATE_DISPATCHED]} outstanding, "
          f"{counts[STATE_ORPHANED]} orphaned, "
          f"{counts[STATE_REFUSED]} refused)")
    for task_id, entry in sorted(ledger.tasks.items()):
        line = (f"    {task_id}  peer={entry.get('peer_id', '')} "
                f"state={entry.get('state', '')} "
                f"runner={entry.get('runner', '')} "
                f"effects={entry.get('effect_class', '')}")
        if entry.get("result_digest"):
            line += f" result={entry['result_digest'][:16]}"
        if entry.get("link"):
            line += f" link={entry['link']}"
        print(line)
    print(f"  events    {len(ledger.events)}")
    for event in ledger.events:
        detail = event.get("link") or event.get("peer_id") or ""
        print(f"    {event.get('at', '')}  {event.get('event', '')} "
              f"{event.get('task_id', '')} {detail}".rstrip())
    return 0


def run_pool_command(args) -> int:
    """`revl run --pool private`: run this composition on a pool member.

    Prints the delivery record and exits 1 on any refusal, naming the link, so
    an operator script reads the status and an operator reads the link."""
    if args.pool != POOL_PRIVATE:  # pragma: no cover - argparse constrains it
        print(f"error: --pool {args.pool!r} is not a pool kind revl operates",
              file=sys.stderr)
        return 2
    missing = [name for name, value in
               (("--pool-dir", args.pool_dir), ("--peer", args.peer),
                ("--peer-addr", args.peer_addr),
                ("--dispatch-identity", args.dispatch_identity),
                ("--attest-identity", args.attest_identity))
               if not value]
    if missing:
        print(f"error: --pool private needs {', '.join(missing)}",
              file=sys.stderr)
        return 2
    ignored = [flag for field, flag, absent in LOCAL_ONLY_RUN_FLAGS
               if getattr(args, field, absent) != absent]
    if ignored:
        print(json.dumps(_refusal(
            LINK_UNSUPPORTED_WITH_POOL,
            f"{', '.join(ignored)} {'is' if len(ignored) == 1 else 'are'} "
            f"honoured by the local runner and cannot cross to a pool member; "
            f"a flag that is accepted and ignored is not implemented. The "
            f"peer's side is chosen with --pool-runner",
            flags=ignored), indent=2, sort_keys=True))
        return 1
    if len(args.files) != 1:
        print(json.dumps(_refusal(
            LINK_MULTI_FILE_ARTIFACT,
            f"a pool task pins ONE artifact by hash and {len(args.files)} "
            f"files were given; a multi-file artifact needs a bundle digest, "
            f"which this slice does not build"), indent=2, sort_keys=True))
        return 1
    try:
        host, port = _split_addr(args.peer_addr)
        source = Path(args.files[0]).read_bytes()
        dispatch_identity = peer_identity.load_private_identity(
            args.dispatch_identity)
        attesting_identity = peer_identity.load_private_identity(
            args.attest_identity)
    except (DispatchError, OSError, ValueError,
            peer_identity.IdentityError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    from .errors import RevlError  # noqa: PLC0415 (lazy)

    try:
        outcome = dispatch_one(
            pool_dir=args.pool_dir, peer_id=args.peer, host=host, port=port,
            source=source, runner=args.pool_runner,
            dispatch_identity=dispatch_identity,
            attesting_identity=attesting_identity,
            timeout=args.pool_timeout)
    except RevlError as error:
        # The artifact does not compile. That is the OPERATOR's own program
        # being wrong, not a peer refusing anything, so it reads like any other
        # `revl` compile failure and no task is written to the ledger.
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (DispatchError, peer_pool.PoolError,
            pool_receipt.ReceiptError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(outcome, indent=2, sort_keys=True))
    return 0 if outcome.get("ok") else 1

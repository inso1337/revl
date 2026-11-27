"""The runtime writer for signed action receipts (issue #2757, second slice).

Design note: `docs/design/570-action-receipt-chain.md`, section 12.

`revl.action_receipt` defines the receipt, the chain and the verifier, and takes
its actions from a caller. This module is the caller that sits at the crossing:
a :class:`ReceiptRecorder` is handed every record the write-ahead log makes
durable (`WriteAheadLog.attach_observer`, installed through
`Recorder.engage_receipts`) and turns the ones that describe a consequential
action into the action mappings `action_receipt.export_chain` signs. The same
class reads a finished log (:func:`recorder_from_records`), so an operator can
export a chain from a WAL an earlier run left, with the same rules as a live run.

Why the WAL is the seam
-----------------------
Every consequential crossing already passes through one writer, in one order,
and it is durable before the crossing fires. A second hook at each emission site
would be a second source of truth that could disagree with the log `revl
recover` trusts. Observing the log means a receipt can only describe a crossing
the log also records, and the order of the chain is the order of the log.

The backend that owns the log is stdlib-only and holds no key, so the observer
reaches it as a bare callable and learns nothing about signing, the same shape
the model-decision sink takes (`revl.model_evidence.CrossingSealer`).

What is a consequential action here
-----------------------------------
Three kinds of record, and no others:

* an ``effect`` record whose boundary class is ``emission``: a boundary crossing,
  journalled ahead of the fire. Its receipt is an ``effect-crossing``.
* a ``deferred-emission`` record that a later ``flushed`` or ``flush-residue``
  record settles: a class-(b) crossing that actually fired at the session
  commit. A deferral that was dropped on abort, or never settled, never crossed
  or may have crossed with no record of it, so it has no receipt; it is counted
  in :attr:`ReceiptRecorder.unsettled`, and both `revl receipts export --wal`
  and `revl run --receipts-out` report that count when it is not zero.
* an ``approval-consumed`` record: the spend of an approval. Its receipt is an
  ``approval-use``, joined to its ``approval-emission`` when the crossing
  returned. A spend whose crossing never returned is recorded, not dropped, with
  capability and component ``unresolved`` and a result that says it did not fire.

Compensations, provisions, in-process effects, fenced ``admit-crossing``
records, cache-hit deliveries and shared reclaims are not receipted in this
slice. The design note says so rather than leaving it to be inferred.

What a commitment covers
------------------------
The input commitment covers exactly the description of the crossing the log
already holds, after the log's own redaction: the crossing label, its service,
key and method, and its arguments with every declared ``Secret[T]`` position
already replaced by the log's placeholder. A placeholder anywhere in the
arguments makes the origin ``secret``, so the commitment is suppressed with that
reason and carries no digest. The digest of an ordinary argument is therefore
over bytes the log already holds in plaintext: a receipt discloses nothing the
log does not. The limit of that statement is also stated: the log records the
declared ``Secret[T]`` positions and no other origin class, so a ``confidential``
value that the log holds in plaintext is salted-committed here. An operator who
shares a chain more widely than the log should know that.

The result commitment covers the recorded outcome of the crossing (the outcome
record's kind and its error text), never a returned value: the log holds none,
by design. The error text is classified like the input: the log redacts a
registered secret inside host text, so a placeholder there suppresses the result.

Fail direction
--------------
:meth:`ReceiptRecorder.observe` is total. A record it cannot read is a recorded
gap in :attr:`ReceiptRecorder.gaps`, never an exception into the run, and
:meth:`ReceiptRecorder.export` REFUSES while a gap stands. A chain with a hole
in it would verify, and a verified chain that omits an action is the failure this
whole feature exists to rule out.
"""

from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping, Optional

from . import action_receipt
from .errors import RevlError
from .taint import REDACTED_SECRET

#: The receipt kinds this writer produces, from the closed set the receipt
#: defines. Restated here as names so a typo is a NameError and not a refusal at
#: the first export.
EFFECT_CROSSING = "effect-crossing"
APPROVAL_USE = "approval-use"

#: What an approval spend names when its crossing never returned, so no
#: capability or component was recorded for it.
UNRESOLVED = "unresolved"

#: The outcome of a journalled crossing that no outcome record settled. The
#: crossing was written ahead of the fire and nothing says how it ended.
OUTCOME_UNSETTLED = "unsettled"

#: Terminal outcomes a deferred emission can have. A record kind other than
#: these settles nothing, so the deferral stays unsettled.
_FLUSHED = "flushed"
_FLUSH_RESIDUE = "flush-residue"

#: The WAL record kinds that settle an ``effect`` emission: the two outcomes
#: `record_emission_outcome` writes and the operator's own resolution
#: (`revl.wal.settled_seqs`). Any record that names the seq of an emission and is
#: one of these ends it.
_EMISSION_OUTCOMES = ("emission-complete", "emission-failed",
                      "operator-resolved")


def _digest64(text: Any) -> Optional[str]:
    """The 64-hex part of a ``sha256:<hex>`` composition digest, or None."""
    if not isinstance(text, str):
        return None
    hex_part = text[len("sha256:"):] if text.startswith("sha256:") else text
    return hex_part if action_receipt._HEX64.match(hex_part) else None


def _carries_placeholder(value: Any, _depth: int = 0) -> bool:
    """Whether ``value`` holds the log's secret placeholder anywhere inside it:
    as a whole value, inside a longer string (the log's text redaction puts it
    there), or as a mapping key (its value redaction renders a secret key as
    the placeholder).

    Conservative on purpose: a substring match suppresses more than an exact
    one, and suppression is the safe direction. A value nested past
    :data:`_MAX_DEPTH` is treated as carrying one, because an unreadable shape
    is the case where a suppressed commitment is safe and a digest is not."""
    if _depth > _MAX_DEPTH:
        return True
    if isinstance(value, str):
        return REDACTED_SECRET in value
    if isinstance(value, Mapping):
        return any(_carries_placeholder(key, _depth + 1)
                   or _carries_placeholder(item, _depth + 1)
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_carries_placeholder(item, _depth + 1) for item in value)
    return False


_MAX_DEPTH = 32


def _origins_of(value: Any) -> list:
    """The origin classes the log can vouch for on ``value``: ``secret`` when a
    declared ``Secret[T]`` position was redacted, and nothing otherwise."""
    return ["secret"] if _carries_placeholder(value) else []


class ReceiptRecorder:
    """Accumulate a session's consequential actions from the records of its
    write-ahead log, and export them as one signed receipt chain.

    ``worker`` names the identity the receipts attribute the actions to (a
    receipt requires one and the log holds none). ``task`` names the task the
    actions belong to and defaults to nothing: a caller that has no task passes
    the session name. ``policy`` is the digest of the policy in force, or None
    when the run had none; it is bound as given and never inferred.
    """

    def __init__(self, *, worker: str, task: str, policy: Optional[str] = None):
        if not isinstance(worker, str) or not worker:
            raise ValueError("a receipt recorder needs a non-empty worker identity")
        if not isinstance(task, str) or not task:
            raise ValueError("a receipt recorder needs a non-empty task")
        if policy is not None and _digest64(policy) is None:
            raise ValueError("policy must be None or a 64-hex digest")
        self.worker = worker
        self.task = task
        self.policy = _digest64(policy) if policy is not None else None
        #: bundle digest in force, from the log's header / generation records.
        #: Each action takes the digest in force when its record was written, so
        #: a chain that spans a `--watch` reload binds each crossing to the
        #: composition that made it.
        self._bundle: Optional[str] = None
        #: journalled emissions not yet settled, by seq
        self._open_effects: dict = {}
        #: deferred emissions not yet settled, by seq
        self._open_deferred: dict = {}
        #: approval spends not yet joined to an emission, by (requestId, use)
        self._open_spends: dict = {}
        #: every action in the order its record was written
        self._actions: list = []
        self._count = 0
        self._aborted = False
        self._committed = False
        self._run_complete = False
        #: human-readable reasons a record could not be accounted for
        self.gaps: list = []

    # -- observing ---------------------------------------------------------

    def observe(self, record: Any) -> None:
        """Account for one durable WAL record. Total: never raises."""
        try:
            self._observe(record)
        except Exception as error:  # noqa: BLE001 - the contract is "never raises"
            self.gaps.append(
                f"a WAL record could not be read ({type(error).__name__}); "
                "the chain would omit whatever it described")

    def _observe(self, record: Any) -> None:
        if not isinstance(record, Mapping):
            self.gaps.append("a WAL record is not an object")
            return
        self._count += 1
        kind = record.get("record")
        if kind in ("header", "generation"):
            self._bundle = _digest64(record.get("composition")) or self._bundle
        elif kind == "effect":
            self._observe_effect(record)
        elif kind == "deferred-emission":
            action = self._deferred_action(record)
            if action is not None and record.get("seq") in self._open_deferred:
                self.gaps.append(
                    f"seq {record.get('seq')!r} was journalled as a deferred "
                    "emission twice with the first never settled, so a flush "
                    "record cannot be told apart between the two")
            elif action is not None:
                self._open_deferred[record.get("seq")] = action
        elif kind in (_FLUSHED, _FLUSH_RESIDUE):
            self._settle_deferred(record)
        elif kind == "approval-consumed":
            self._observe_spend(record)
        elif kind == "approval-emission":
            self._join_emission(record)
        elif kind == "commit-approved":
            self._committed = True
        elif kind == "aborted":
            self._aborted = True
        elif kind == "run-complete":
            self._run_complete = True
        elif kind in _EMISSION_OUTCOMES:
            self._settle_effect(record)

    def _observe_effect(self, record: Mapping) -> None:
        boundary = record.get("boundary")
        if not isinstance(boundary, Mapping) or boundary.get("class") != "emission":
            if record.get("kind") == "emission":
                # a record that says it is a crossing and does not describe one
                # is not an in-process effect to skip: it is a crossing this
                # writer cannot account for
                self.gaps.append(
                    f"an emission record (seq {record.get('seq')!r}) carries no "
                    "emission boundary, so no receipt can say what crossed")
            return
        detail = boundary.get("detail")
        detail = detail if isinstance(detail, Mapping) else {}
        label, component = record.get("label"), record.get("component")
        if not (isinstance(label, str) and label
                and isinstance(component, str) and component):
            self.gaps.append(
                f"an emission record (seq {record.get('seq')!r}) names no "
                "component or crossing label, so no receipt can say what "
                "crossed")
            return
        crossing = {
            "crossing": label,
            "key": detail.get("key"),
            "method": detail.get("method"),
            "service": detail.get("service"),
            # a copy: the record is the log's, by reference, and the digest is
            # taken at export, so a later mutation must not reach it
            "args": copy.deepcopy(detail.get("args")),
        }
        action = {
            "receipt_kind": EFFECT_CROSSING,
            "component": component,
            "capability": label,
            "approval": None,
            "input": crossing,
            "input_origins": _origins_of(crossing),
            "result": {"outcome": OUTCOME_UNSETTLED},
            "result_origins": [],
            "bundle": self._bundle,
            "_order": self._count,
        }
        if record.get("seq") in self._open_effects:
            self.gaps.append(
                f"seq {record.get('seq')!r} was journalled as an emission twice "
                "with the first never settled, so an outcome record cannot be "
                "told apart between the two crossings")
            return
        self._open_effects[record.get("seq")] = action
        self._actions.append(action)

    def _settle_effect(self, record: Mapping) -> None:
        action = self._open_effects.pop(record.get("seq"), None)
        if action is None:
            return
        result = {"outcome": record.get("record")}
        if record.get("error"):
            result["error"] = record.get("error")
        action["result"] = result
        # the error text can carry the placeholder (the log redacts a registered
        # secret inside host text), so the result is classified like the input
        action["result_origins"] = _origins_of(result)

    def _deferred_action(self, record: Mapping) -> Optional[dict]:
        call = record.get("call")
        call = call if isinstance(call, Mapping) else {}
        origin = record.get("origin")
        origin = origin if isinstance(origin, Mapping) else {}
        label = ".".join(str(part) for part in
                         (call.get("receiver"), call.get("method"))
                         if part is not None)
        if not label:
            self.gaps.append(
                f"a deferred emission (seq {record.get('seq')!r}) names no "
                "receiver or method, so no receipt can say what crossed")
            return None
        crossing = {
            "crossing": label,
            "key": call.get("receiver"),
            "method": call.get("method"),
            "service": None,
            "args": copy.deepcopy(call.get("args")),
        }
        return {
            "receipt_kind": EFFECT_CROSSING,
            "component": origin.get("component") or UNRESOLVED,
            "capability": label,
            "approval": None,
            "input": crossing,
            "input_origins": _origins_of(crossing),
            "result": {"outcome": OUTCOME_UNSETTLED},
            "result_origins": [],
            "bundle": self._bundle,
        }

    def _settle_deferred(self, record: Mapping) -> None:
        action = self._open_deferred.pop(record.get("seq"), None)
        if action is None:
            return
        result = {"outcome": record.get("record")}
        if record.get("error"):
            result["error"] = record.get("error")
        action["result"] = result
        # the error text can carry the placeholder (the log redacts a registered
        # secret inside host text), so the result is classified like the input
        action["result_origins"] = _origins_of(result)
        action["_order"] = self._count
        self._actions.append(action)

    def _observe_spend(self, record: Mapping) -> None:
        spend = {
            "requestId": record.get("requestId"),
            "use": record.get("use"),
            "scope": record.get("scope"),
        }
        action = {
            "receipt_kind": APPROVAL_USE,
            "component": UNRESOLVED,
            "capability": UNRESOLVED,
            "approval": {"request_id": record.get("requestId"),
                         "use": None if record.get("use") is None
                         else str(record.get("use"))},
            "input": spend,
            "input_origins": [],
            "result": {"fired": False},
            "result_origins": [],
            "bundle": self._bundle,
            "_order": self._count,
        }
        key = (record.get("requestId"), record.get("use"))
        self._open_spends.setdefault(key, []).append(action)
        self._actions.append(action)

    def _join_emission(self, record: Mapping) -> None:
        key = (record.get("requestId"), record.get("use"))
        waiting = self._open_spends.get(key)
        if not waiting:
            self.gaps.append(
                f"an approval-emission for request {record.get('requestId')!r} "
                "matches no approval spend, so no receipt can say which "
                "approval authorised it")
            return
        action = waiting.pop(0)
        if not waiting:
            del self._open_spends[key]
        action["component"] = record.get("component") or UNRESOLVED
        action["capability"] = record.get("capability") or UNRESOLVED
        action["result"] = {"fired": True}

    # -- reading what was observed ----------------------------------------

    @property
    def unsettled(self) -> int:
        """Deferred emissions that were journalled and never settled: dropped on
        abort, or cut by a crash. They never crossed, so they have no receipt;
        this counts them so their absence is stated."""
        return len(self._open_deferred)

    def session_verdict(self) -> Optional[str]:
        """``committed`` or ``aborted`` when the log says which, else None.

        The log says it three ways and no more: ``commit-approved`` is the
        commit verdict (`docs/design/245-session-commit.md`), an ``aborted``
        record is a completed abort, and neither is the unknown a caller must
        resolve. A log with both is refused as contradictory by returning None.
        A plain run that completed and wrote neither is not guessed at."""
        if self._committed and self._aborted:
            return None
        if self._committed:
            return "committed"
        if self._aborted:
            return "aborted"
        return None

    def actions(self) -> list:
        """The consequential actions observed so far, in the order their
        records were written, as the mappings `export_chain` signs."""
        ordered = sorted(self._actions, key=lambda action: action["_order"])
        return [{name: value for name, value in action.items()
                 if name != "_order"}
                | {"policy": self.policy, "task": self.task,
                   "worker": self.worker}
                for action in ordered]

    def export(self, key: bytes, *, session: str,
               session_verdict: Optional[str] = None,
               salt: Optional[str] = None, now: Optional[str] = None) -> dict:
        """Sign the observed actions as one chain.

        ``session_verdict`` is the caller's when it knows the outcome (a run
        that failed, a session the host ended); otherwise the log's own
        (:meth:`session_verdict`). With neither, this refuses: the verdict is
        bound into every receipt and a chain that does not say how its session
        ended is not exported on a guess.

        Refuses while :attr:`gaps` is non-empty, for the reason in the module
        header."""
        if self._committed and self._aborted:
            raise RevlError(
                "<action-receipt>", 0,
                "the log records both commit-approved and an aborted marker, "
                "so it contradicts itself about how the session ended and no "
                "verdict is bound to it")
        if self.gaps:
            raise RevlError(
                "<action-receipt>", 0,
                "the receipt chain would be incomplete: "
                + "; ".join(self.gaps[:3])
                + (f" (and {len(self.gaps) - 3} more)"
                   if len(self.gaps) > 3 else ""))
        verdict = session_verdict or self.session_verdict()
        if verdict is None:
            raise RevlError(
                "<action-receipt>", 0,
                "the log records neither commit-approved nor an aborted "
                "marker, so the session verdict is not derivable; pass the "
                "verdict the session ended with")
        return action_receipt.export_chain(
            key, session=session, actions=self.actions(),
            session_verdict=verdict, salt=salt, now=now)


def recorder_from_records(records: Iterable[Any], *, worker: str, task: str,
                          policy: Optional[str] = None) -> ReceiptRecorder:
    """A recorder that has observed every record in ``records`` in order: the
    offline path, for a log an earlier run left. Same rules as a live run."""
    recorder = ReceiptRecorder(worker=worker, task=task, policy=policy)
    for record in records:
        recorder.observe(record)
    return recorder


def recorder_from_wal(loaded: Mapping, *, worker: str, task: str,
                      policy: Optional[str] = None) -> ReceiptRecorder:
    """A recorder over a log as `revl.wal.read_wal` returns it.

    `read_wal` hands the header back apart from the records, and the header is
    what names the composition a receipt's bundle digest comes from, so it is
    observed first, exactly as the live writer observes it."""
    header = loaded.get("header")
    ordered = ([header] if isinstance(header, Mapping) and header else [])
    ordered.extend(loaded.get("records") or [])
    return recorder_from_records(ordered, worker=worker, task=task,
                                 policy=policy)


__all__ = [
    "APPROVAL_USE",
    "EFFECT_CROSSING",
    "OUTCOME_UNSETTLED",
    "ReceiptRecorder",
    "UNRESOLVED",
    "recorder_from_records",
    "recorder_from_wal",
]

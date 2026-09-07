"""Reference-tier runtime minting for the `shared` ownership mode (item 308 S1,
issue #96).

The `shared` frontend admission (`parser.py`/`lower.py`) and the whole-process
crash reclaim (`recovery.py: recover_shared_grants`) were wired first; the
primitive that actually counts holders and binds the declared inverse to the
count's zero crossing is `liveness_confirm.SharedGrantBook`. What was missing
between them was the RUNTIME that mints the counted grant as a program runs and
JOURNALS the durable ledger the crash reclaim reads back.

This module is that runtime for the py reference tier. It is the counterpart to
`recover_shared_grants`'s reader half: it appends the exact records that reader
consumes —

  * ``shared-grant`` on the mint, on every consume (a crossing into another
    activation's scope), and on every release — each one a durable, fsync'd
    ledger write whose ``holders`` field is the live holder set AFTER the
    operation (consume-before-fire, 243 rule 4, the same discipline
    ``replay-fence`` uses). The LATEST ``shared-grant`` per handle is therefore
    the reconstructible count, which is exactly what the reader keys on;
  * ``shared-complete`` on the orderly last release, appended BEFORE the
    zero-crossing inverse fires, so a recover run finds the completion and the
    shared-reclaim path re-fires NOTHING (the orderly inverse is an ordinary
    LIFO bracket entry in the last releaser's frame, its exactly-once owned by
    the bracket channel, not by the crash reclaim).

The 294-lease binding the design recommends over a bespoke refcount lives in
`SharedGrantBook`; this module only adds the durable-ledger journaling around
it, so a whole-process crash mid-count leaves a ledger the shipped `revl
recover` reads and reclaims. Cross-tier minting (the go/rust/java/wasm
emitters producing the same ledger writes) is sequenced with item 294 and is
NOT here; the reference tier proves the property.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional

from .liveness_confirm import LivenessProbe, ReclaimReport, SharedGrantBook

__all__ = ["JournaledSharedGrantBook"]


class JournaledSharedGrantBook:
    """A :class:`SharedGrantBook` whose holder count is a durable ledger.

    Wraps the in-memory primitive and appends a ``shared-grant`` /
    ``shared-complete`` record to a write-ahead log on every state change, so
    the count survives a whole-process crash and `revl recover` can re-fire the
    zero-crossing inverse exactly once. The `world` applies a serialized inverse
    op (``{receiver, method, args}``) the same way `recovery.World.apply_inverse`
    does, so the orderly in-frame fire and the crash reclaim run the identical
    close.
    """

    def __init__(self, wal_path: str, *, world: Any, ttl: float = 30.0) -> None:
        self._wal_path = wal_path
        self._world = world
        self._book = SharedGrantBook(ttl=ttl)
        #: handle -> the serialized inverse op, the durable form of the close
        self._inverse_op: dict[str, dict] = {}
        #: handles whose zero crossing has a TERMINAL durable record written
        #: here — a ``shared-complete`` on a confirmed close, or a
        #: ``shared-reclaim-fence`` on an attempted-but-unconfirmed one. Guards
        #: the live-reclaim finalizer from re-settling a crossing it (or the
        #: orderly path) already settled, so a faulted close is never later
        #: overwritten with a false completion (issues #709/#710).
        self._settled: set[str] = set()
        #: handles whose ``shared-reclaim-fence`` is already durable. One fence
        #: per handle is all recovery needs (it keys on presence, not count), so
        #: this keeps `_journal_fence` idempotent: the orderly path fences
        #: explicitly before it fires and the bound inverse also fences on the
        #: way to the effect, and the two must not stack a second fence record.
        self._fenced: set[str] = set()

    # -- durable ledger ----------------------------------------------------

    def _append(self, record: dict) -> None:
        """Append one JSONL record and fsync it. Same seal-torn-tail-then-append
        discipline `recovery._append_shared_reclaim_fence` uses, so a mint
        interleaved with any other durable writer leaves a well-formed log."""
        from .wal import seal_torn_tail  # noqa: PLC0415 — tier-agnostic core
        seal_torn_tail(self._wal_path)
        with open(self._wal_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except (OSError, ValueError):  # pragma: no cover — e.g. a pipe target
                pass

    def _journal_grant(self, handle: str) -> None:
        grant = self._book.grant(handle)
        holders = sorted(grant.holders) if grant is not None else []
        self._append({"record": "shared-grant", "handle": handle,
                      "inverse": self._inverse_op[handle], "holders": holders})

    def _journal_fence(self, handle: str) -> None:
        """Durably fence a zero crossing whose inverse was ATTEMPTED but whose
        success is not confirmed — an orderly fire about to run, or a live
        reclaim whose inverse raised. This is the same ``shared-reclaim-fence``
        record `revl recover` reads as 'fenced-before-attempt, outcome unknown':
        it proves an attempt was ABOUT to start, never that it completed, so a
        later recover over the same durable ledger re-fires NOTHING and the
        failed/unknown evidence survives the restart (consume-before-fire, the
        discipline the WAL seal in #642/#695 and `recover`'s own reclaim fence
        use).

        Idempotent per handle: a handle's fence is written at most once for the
        life of this book, so the orderly path's explicit pre-fire fence and the
        bound inverse's own on-the-way-to-the-effect fence collapse to one
        record rather than stacking a redundant second one."""
        if handle in self._fenced:
            return
        self._append({"record": "shared-reclaim-fence", "handle": handle})
        self._fenced.add(handle)

    def _inverse_callable(self, handle: str) -> Callable[[], None]:
        op = self._inverse_op[handle]

        def _fire() -> None:
            # Write-ahead the reclaim intent at the NARROWEST point: a durable
            # ``shared-reclaim-fence`` (an inverse is owed / about to run) is
            # forced to disk BEFORE the external effect, for whoever fires — the
            # orderly last releaser, or the primitive's out-of-frame crash
            # reclaim. So a crash between the effect and any completion write
            # always leaves a fence a later recover reads as outcome-unknown
            # residue: it never re-fires the inverse a second time (#709) and
            # never misreads the crossing as a clean balance (#710). Idempotent,
            # so an explicit pre-fire fence upstream makes this a no-op.
            self._journal_fence(handle)
            self._world.apply_inverse(op)

        return _fire

    # -- the counted grant lifecycle --------------------------------------

    def mint(self, handle: str, inverse_op: dict, holder_id: str, *,
             now: float | None = None) -> None:
        """The `shared` acquire: mint (or, for a second acquirer, join) the
        counted grant, register `holder_id` as holder #1's peer, and journal the
        opening ledger write. The FIRST acquirer binds the declared inverse."""
        self._inverse_op.setdefault(handle, inverse_op)
        self._book.acquire(handle, self._inverse_callable(handle), holder_id, now=now)
        self._journal_grant(handle)

    def consume(self, handle: str, holder_id: str, *,
                now: float | None = None) -> None:
        """A crossing into another activation's scope (B1's crossing
        enumeration): a lease CONSUME, count += 1, a durable ledger write. The
        same `SharedGrantBook.acquire` join the mint uses, so a handle that
        reaches a new scope is a counted holder there."""
        if handle not in self._inverse_op:
            from .liveness_confirm import LivenessError  # noqa: PLC0415
            raise LivenessError(
                f"no shared grant on handle `{handle}` to consume (item 308 S1)")
        self._book.acquire(handle, self._inverse_callable(handle), holder_id,
                           now=now)
        self._journal_grant(handle)

    def beat(self, holder_id: str, *, now: float | None = None) -> None:
        """A holder heartbeat, forwarded to the liveness registry."""
        self._book.beat(holder_id, now=now)

    def release(self, handle: str, holder_id: str, *,
                now: float | None = None) -> bool:
        """A receiving scope's teardown: a lease RELEASE, count -= 1, a durable
        ledger write. Returns True exactly when this release drove the count to
        zero (the orderly path), in which case the declared inverse runs HERE, in
        the last releaser's frame. The completion marker is journaled only AFTER
        the inverse returns, behind a durable attempt fence, so a crash reclaim
        never double-closes it and a raising inverse is never recorded as done.
        """
        inverse = self._book.release(handle, holder_id, now=now)
        if inverse is None:
            # a plain decrement: the count is still positive (or already fired).
            # Journal the new live set; a crash here still shows holders > 0 and
            # the reclaim re-fires once, or shows the count the survivors hold.
            self._journal_grant(handle)
            return False
        # the zero crossing, orderly path. Write-ahead the intent FIRST: the
        # durable ``shared-reclaim-fence`` (an inverse is owed) precedes BOTH the
        # count-zero ledger write AND the fire, so no durable state ever shows a
        # zeroed count — or an attempted inverse — without a fence proving one
        # was owed. Ordering the fence before the count-zero write is what closes
        # #710: a crash between the two used to leave a durable holders == []
        # with no fence, which recover misread as a balanced accumulator and
        # dropped the owed inverse; now that gap is fence-then-zero, so recover
        # always finds the fence and reports the crossing (never a false clean).
        # The ``shared-complete`` marker still lands ONLY after the inverse
        # returns, so a raising/crashing inverse between fence and completion
        # stays 'fenced-before-attempt, outcome unknown' — never a concealed
        # 'done', never a blind re-fire. The orderly inverse's exactly-once stays
        # the bracket channel's; the fence only fails a later recover CLOSED.
        self._journal_fence(handle)  # intent, BEFORE the destructive count-zero write
        self._settled.add(handle)    # terminal durable state is owned here now
        self._journal_grant(handle)  # holders == [] now, but the fence already stands
        inverse()                    # only past the durable fence (self-fence is a no-op)
        self._append({"record": "shared-complete", "handle": handle})
        return True

    def reclaim_crashed(self, *, probe: LivenessProbe,
                        now: float | None = None) -> ReclaimReport:
        """The live-process crash path, delegated to the primitive: for every
        holder the `probe` confirms gone, decrement; a grant that reaches zero
        with no live holder left fires its inverse out of frame exactly once and
        is reported as `reclaim` residue. A grant a live holder still owns is
        left standing. The ledger is re-journaled to reflect the survivors.

        A handle the primitive drove to its zero crossing here has ALREADY run
        (or attempted) its inverse out of frame, exactly once, fenced by
        ``grant.fired``. Finalizing that crossing in the durable ledger is this
        wrapper's job: without it the pre-reclaim ``shared-grant`` record still
        names live holders and no completion, so a later `revl recover` reads it
        as standing residue and re-fires the inverse a second time (issue #709).
        So each freshly fired handle gets a terminal record: a ``shared-complete``
        on a confirmed-clean close, or — when the reclaim inverse RAISED (the
        handle is in the report's faults) — a ``shared-reclaim-fence`` that keeps
        the outcome failed/unknown and blocks an automatic recover replay. The
        per-handle exactly-once fence and the independence of distinct
        handles/owners (#671/#691) are the primitive's and are left untouched."""
        report = self._book.reclaim_crashed(probe=probe, now=now)
        faulted = {f.get("handle") for f in report.faults if f.get("handle")}
        for handle in list(self._inverse_op):
            grant = self._book.grant(handle)
            if grant is None:
                continue
            if not grant.fired:
                # a grant a live holder still owns: re-journal the survivor set,
                # so the durable count stays honest and a later whole-process
                # crash reclaims it once.
                self._journal_grant(handle)
                continue
            if handle in self._settled:
                # already given a terminal record (an orderly release, or an
                # earlier reclaim pass): never re-settle it — that could paper a
                # faulted close over with a false completion.
                continue
            if handle in faulted:
                # the live reclaim inverse raised: retain failed/unknown
                # evidence, no clean completion and no automatic recover replay.
                self._journal_fence(handle)
            else:
                # a confirmed-clean live reclaim: journal the zero-crossing count
                # and its completion, the same terminal record the orderly last
                # release writes, so recover owes nothing on this handle.
                self._journal_grant(handle)  # holders == [] now
                self._append({"record": "shared-complete", "handle": handle})
            self._settled.add(handle)
        return report

    # -- reads -------------------------------------------------------------

    def count(self, handle: str) -> int:
        return self._book.count(handle)

    def holders(self, handle: str) -> set[str]:
        grant = self._book.grant(handle)
        return set(grant.holders) if grant is not None else set()

    def fired(self, handle: str) -> bool:
        grant = self._book.grant(handle)
        return bool(grant is not None and grant.fired)

    @property
    def book(self) -> SharedGrantBook:
        return self._book


def ledger_count(wal_records: list[dict], handle: str) -> Optional[list[str]]:
    """Reconstruct a shared handle's holder set from a durable WAL, the way
    `recover_shared_grants` does: the LATEST ``shared-grant`` per handle carries
    the count at the last ledger write. Returns the sorted holder list, or None
    when the handle was never minted. A ``shared-complete`` means the orderly
    path already ran the inverse, so the reconstructed count is empty."""
    latest: Optional[dict] = None
    completed = False
    for r in wal_records:
        if r.get("record") == "shared-grant" and r.get("handle") == handle:
            latest = r
        elif r.get("record") == "shared-complete" and r.get("handle") == handle:
            completed = True
    if latest is None:
        return None
    if completed:
        return []
    return sorted(latest.get("holders") or [])

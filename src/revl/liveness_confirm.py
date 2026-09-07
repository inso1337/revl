"""Process-liveness confirmation — the primitive `shared` teardown was blocked on
(roadmap item 308, S1 crash-path; issue #96).

WHY THIS MODULE EXISTS. Item 308's `shared` mode binds a resource's declared
inverse to a holder count reaching zero: N holders take a handle, the last one
to let go runs the close exactly once. The orderly path is an ordinary
`bracket` entry (the last releaser's LIFO stack, G5/G7 unchanged). The CRASH
path is the hard part the design (docs/design/308-effect-ownership-modes.md, S1)
left owed, and it owed a runtime shape that did not exist:

  A holder that is SIGKILLed without releasing must not pin the handle forever
  (a bespoke refcount does exactly that, which is why it loses to a lease), but
  the inverse also must NOT fire on a bare wall-clock ttl. Expiring a *grant*
  on wall-clock is fail-safe: the holder loses an authority and re-requests
  (that is `mcp/leases.py`'s `LeaseBook`, item 61/294 — it drops a grant lazily
  on read and never fires anything). Firing an *inverse* on wall-clock is
  fail-DANGEROUS: it closes a handle a slow-but-alive holder still holds, which
  manufactures the exact use-after-close 308 exists to refuse.

  So the backstop must be LIVENESS-GATED: a ttl lapse only ARMS the reclaim,
  and the inverse fires only once the runtime CONFIRMS the holder is gone
  (process dead, activation torn down without release). A slow holder pins the
  handle until it releases or dies. That is the correct bias for teardown.

Neither existing surface provides that positive confirmation:

  * `revl.liveness` is item 438's Petri-net deadlock analysis — a STATIC search
    over a composition's marking graph. It answers "can this composition reach a
    dead state", not "is that process still breathing".
  * `revl.mcp.leases.LeaseBook` is deliberately FAIL-SAFE: `_prune` drops an
    expired lease and stamps an `expired` event, and that is all. It never runs
    an inverse, by design, because a lease governs *who may replace a component*,
    not *when a resource is closed*.

This module is the missing third surface: a positive, gated confirmation that a
holder is alive, and — on confirmed loss — an EXACTLY-ONCE reclaim that fires the
declared inverse out of frame and reports a `reclaim` residue record, distinct
from a `bracket-fault` (an orderly bracket that raised). It is the executor S1
named as owed.

DELIBERATELY SELF-CONTAINED, exactly as `LeaseBook` is. It holds no cordis
runtime handle and closes nothing itself; it is pure bookkeeping over a clock
plus a pluggable :class:`LivenessProbe`, so the whole confirmation/reclaim
discipline can be reasoned about — and tested — without booting a process. The
one side effect, firing the bound inverse, is a callable the caller supplies;
the module decides WHEN (armed by ttl, gated by the probe, fenced to once) and
records WHAT happened, never how the close is performed.

THE FENCE. A reclaim is fired at most once per holder. `arm`/`confirm`/`fire`
walk a holder through a small state machine (`ALIVE -> ARMED -> RECLAIMED`), and
the transition into `RECLAIMED` is the fence: a second `reclaim()` sweep, a
duplicate crash signal, or a `revl recover` re-run over the same durable ledger
all find the holder already reclaimed and fire nothing. This is the same
consume-before-fire discipline `recovery.py`'s replay/reissue fences use.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

# ---------------------------------------------------------------- probe

# A holder's declared inverse: the close bound to the shared count's zero
# crossing. Runs out of frame on the crash path, so it takes no arguments the
# dead frame would have supplied — the handle it closes is captured when the
# grant is minted, exactly as a `bracket` entry captures its referent.
Inverse = Callable[[], None]


class LivenessProbe:
    """Positive confirmation of whether a holder's process is still alive.

    This is the seam the fail-safe/fail-dangerous distinction turns on. A ttl
    lapse ARMS a reclaim; the probe is what CONFIRMS the holder is actually gone
    before the inverse fires. The default is FAIL-SAFE toward the running holder:
    an unknown holder is presumed ALIVE, so an unconfigured or flaky probe pins
    the handle rather than closing a resource out from under a live process. A
    reclaim needs a POSITIVE "gone", never merely the absence of a "here"."""

    def alive(self, holder_id: str) -> bool:  # pragma: no cover — interface
        """True if the holder is confirmed alive; False only on a positive
        confirmation that it is gone. Ambiguity resolves to True (pin, do not
        close) — the fail-safe direction for teardown."""
        return True


class DictProbe(LivenessProbe):
    """A probe backed by an explicit liveness map — the test/bookkeeping double,
    and the shape a real probe (a pid liveness check, a heartbeat table, a
    process-supervisor query) presents to this module.

    A holder is confirmed gone only when it has been explicitly `kill`ed; a
    holder the probe has never heard of is presumed ALIVE (fail-safe), so a
    reclaim cannot fire on ignorance."""

    def __init__(self) -> None:
        self._dead: set[str] = set()

    def kill(self, holder_id: str) -> None:
        """Record a positive confirmation that ``holder_id``'s process is gone."""
        self._dead.add(holder_id)

    def revive(self, holder_id: str) -> None:
        self._dead.discard(holder_id)

    def alive(self, holder_id: str) -> bool:
        return holder_id not in self._dead


# ---------------------------------------------------------------- model

# Holder lifecycle. The transition into RECLAIMED is the exactly-once fence.
_ALIVE = "alive"          # beating, or within its deadline
_ARMED = "armed"          # ttl lapsed; reclaim is armed but NOT yet confirmed
_RELEASED = "released"    # let go orderly; no reclaim owed
_RECLAIMED = "reclaimed"  # confirmed gone and its inverse fired — terminal


@dataclass
class Holder:
    """One registered holder of a liveness-gated resource.

    ``deadline`` is an absolute wall-clock epoch: the holder is presumed alive
    while ``now < deadline`` and ARMS a reclaim once past it (a `beat` pushes the
    deadline out). ``state`` walks ALIVE -> ARMED -> RECLAIMED (or -> RELEASED on
    an orderly let-go); RECLAIMED and RELEASED are terminal and fence any further
    firing."""

    holder_id: str
    registered: float
    deadline: float
    state: str = _ALIVE


class LivenessRegistry:
    """The holders of one liveness-gated resource, and the exactly-once reclaim
    over them.

    Keyed by holder id. Like :class:`~revl.mcp.leases.LeaseBook` it is pure
    bookkeeping over an injected clock (``now``) and an injected
    :class:`LivenessProbe`; every mutation stamps an event for the causal trace
    (item 27), channel ``liveness`` so a trace query can select the
    confirmation story out of the lifecycle stream."""

    def __init__(self, *, ttl: float = 30.0) -> None:
        if ttl <= 0:
            raise ValueError(f"a liveness ttl must be positive (got {ttl})")
        self._ttl = float(ttl)
        self._holders: dict[str, Holder] = {}
        self.events: list[dict] = []

    # -- trace -------------------------------------------------------------

    def _stamp(self, action: str, holder: Holder, now: float,
               detail: str) -> dict:
        event = {"channel": "liveness", "subject": holder.holder_id,
                 "action": action, "state": holder.state, "at": now,
                 "detail": detail}
        self.events.append(event)
        return event

    def drain_events(self) -> list[dict]:
        events, self.events = self.events, []
        return events

    # -- registration & heartbeat -----------------------------------------

    def register(self, holder_id: str, *, ttl: float | None = None,
                 now: float | None = None) -> Holder:
        """Register a holder (or renew one already alive). A registered holder is
        presumed alive for ``ttl`` seconds; a `beat` renews that window."""
        now = time.time() if now is None else now
        ttl = self._ttl if ttl is None else float(ttl)
        if ttl <= 0:
            raise ValueError(f"a liveness ttl must be positive (got {ttl})")
        existing = self._holders.get(holder_id)
        if existing is not None and existing.state == _RECLAIMED:
            raise LivenessError(
                f"holder `{holder_id}` was already reclaimed — its resource was "
                f"closed on confirmed loss and cannot be re-registered under the "
                f"same id (item 308 S1; a reclaim is terminal, fenced once)")
        holder = Holder(holder_id, now, now + ttl)
        self._holders[holder_id] = holder
        self._stamp("register", holder, now,
                    f"holder `{holder_id}` registered, alive for {ttl}s")
        return holder

    def beat(self, holder_id: str, *, ttl: float | None = None,
             now: float | None = None) -> Holder:
        """A heartbeat: the holder is alive, so push its deadline out. Refuses a
        beat from a reclaimed holder (its resource is already closed) — a late
        heartbeat from a process the runtime confirmed gone must not resurrect a
        closed handle."""
        now = time.time() if now is None else now
        ttl = self._ttl if ttl is None else float(ttl)
        holder = self._holders.get(holder_id)
        if holder is None:
            raise LivenessError(
                f"no registered holder `{holder_id}` to beat — register first "
                f"(item 308 S1 liveness confirmation)")
        if holder.state == _RECLAIMED:
            raise LivenessError(
                f"holder `{holder_id}` was reclaimed on confirmed loss; a late "
                f"heartbeat cannot reopen a closed resource (item 308 S1)")
        if holder.state == _RELEASED:
            raise LivenessError(
                f"holder `{holder_id}` already released its hold (item 308 S1)")
        holder.deadline = now + ttl
        # a beat from a holder that had ARMED (a slow-but-alive holder that came
        # back before confirmation) DISARMS it: it is alive after all.
        if holder.state == _ARMED:
            holder.state = _ALIVE
            self._stamp("disarm", holder, now,
                        f"holder `{holder_id}` beat while armed — alive after "
                        f"all, reclaim disarmed")
        else:
            self._stamp("beat", holder, now, f"holder `{holder_id}` heartbeat")
        return holder

    def release(self, holder_id: str, *, now: float | None = None) -> Holder:
        """An ORDERLY let-go. The holder is done with the handle and did not
        crash; no reclaim is owed (the orderly zero-crossing inverse rides the
        releaser's own LIFO bracket, not this executor). Terminal."""
        now = time.time() if now is None else now
        holder = self._holders.get(holder_id)
        if holder is None:
            raise LivenessError(
                f"no registered holder `{holder_id}` to release (item 308 S1)")
        if holder.state == _RECLAIMED:
            raise LivenessError(
                f"holder `{holder_id}` was already reclaimed on confirmed loss "
                f"(item 308 S1)")
        holder.state = _RELEASED
        self._stamp("release", holder, now,
                    f"holder `{holder_id}` released its hold orderly")
        return holder

    # -- confirmation & reclaim -------------------------------------------

    def confirmed_alive(self, holder_id: str, *,
                        probe: LivenessProbe | None = None,
                        now: float | None = None) -> bool:
        """Is this holder confirmed alive right now?

        The query a `shared` handoff runs before proceeding: it is safe to pass a
        handle to / rely on a peer only while the peer is confirmed alive. True
        while within the deadline; past the deadline it consults the probe for a
        POSITIVE confirmation — a holder past its ttl but whose process the probe
        reports alive is a slow-but-alive holder and stays alive (pin, do not
        reclaim). A reclaimed or released holder is not alive."""
        now = time.time() if now is None else now
        holder = self._holders.get(holder_id)
        if holder is None or holder.state in (_RECLAIMED, _RELEASED):
            return False
        if now < holder.deadline:
            return True
        probe = probe if probe is not None else LivenessProbe()
        return probe.alive(holder_id)

    def arm_expired(self, now: float | None = None) -> list[Holder]:
        """Move every alive holder past its deadline to ARMED. This is the
        fail-SAFE half: a lapsed ttl only ARMS — it never closes anything. The
        fail-dangerous close waits for :meth:`reclaim`'s probe confirmation."""
        now = time.time() if now is None else now
        armed: list[Holder] = []
        for holder in self._holders.values():
            if holder.state == _ALIVE and now >= holder.deadline:
                holder.state = _ARMED
                self._stamp("arm", holder, now,
                            f"holder `{holder.holder_id}` ttl lapsed — reclaim "
                            f"armed, awaiting liveness confirmation")
                armed.append(holder)
        return armed

    def reclaim(self, inverses: dict[str, Inverse], *,
                probe: LivenessProbe | None = None,
                now: float | None = None) -> "ReclaimReport":
        """Fire the bound inverse for every holder CONFIRMED gone, exactly once.

        The whole gated discipline in one call:

          1. ARM  — any alive holder past its ttl is armed (fail-safe; no close).
          2. GATE — an armed holder is reclaimed only if the ``probe`` returns a
             positive "gone". A holder past ttl but confirmed alive (slow) is
             left ARMED and pins its handle; it disarms on its next `beat`.
          3. FIRE — the bound inverse in ``inverses[holder_id]`` runs out of
             frame. Success drives the holder to RECLAIMED (the fence: a later
             sweep or a `revl recover` re-run fires nothing). A raising inverse
             is a `reclaim-fault` residue, NOT a `bracket-fault` (a bracket-fault
             is an orderly LIFO close that raised; this close had no frame).

        Returns a :class:`ReclaimReport` whose residue is CLEAN when every
        confirmed-gone holder's inverse fired, and carries the fault set
        otherwise. A holder with no entry in ``inverses`` is a caller error (the
        grant was minted without binding its close) and is reported as an
        unbound-inverse residue rather than silently skipped."""
        now = time.time() if now is None else now
        probe = probe if probe is not None else LivenessProbe()
        self.arm_expired(now)
        fired: list[str] = []
        pinned: list[str] = []
        faults: list[dict] = []
        for holder in list(self._holders.values()):
            if holder.state != _ARMED:
                continue
            if probe.alive(holder.holder_id):
                # confirmed alive after arming: slow-but-alive, pin the handle.
                pinned.append(holder.holder_id)
                self._stamp("pin", holder, now,
                            f"holder `{holder.holder_id}` armed but confirmed "
                            f"alive — pinned, no reclaim")
                continue
            inverse = inverses.get(holder.holder_id)
            if inverse is None:
                faults.append({"holder": holder.holder_id,
                               "kind": "unbound-inverse",
                               "detail": (f"holder `{holder.holder_id}` confirmed "
                                          f"gone but no inverse was bound to it")})
                continue
            try:
                inverse()
            except Exception as error:  # noqa: BLE001 — recorded as residue
                faults.append({"holder": holder.holder_id,
                               "kind": "reclaim-fault",
                               "detail": (f"the reclaim inverse for holder "
                                          f"`{holder.holder_id}` raised: {error}")})
                # a raised reclaim is still fenced: it does not re-fire on the
                # next sweep. The residue records the owed, unfinished close.
                holder.state = _RECLAIMED
                self._stamp("reclaim-fault", holder, now,
                            f"holder `{holder.holder_id}` reclaim inverse raised")
                continue
            holder.state = _RECLAIMED
            fired.append(holder.holder_id)
            self._stamp("reclaim", holder, now,
                        f"holder `{holder.holder_id}` confirmed gone — inverse "
                        f"fired once, resource reclaimed")
        return ReclaimReport(fired=fired, pinned=pinned, faults=faults, at=now)

    # -- reads -------------------------------------------------------------

    def holder(self, holder_id: str) -> Holder | None:
        return self._holders.get(holder_id)

    def live_ids(self, *, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        return sorted(h.holder_id for h in self._holders.values()
                      if h.state in (_ALIVE, _ARMED))


class LivenessError(RuntimeError):
    """A liveness operation the registry refuses (beating a reclaimed holder,
    releasing an unknown one). The MCP/server layer maps it to the same
    ``ok: false`` refusal shape every other error here carries."""


@dataclass
class ReclaimReport:
    """The verdict of a :meth:`LivenessRegistry.reclaim` sweep.

    ``fired`` reclaimed cleanly; ``pinned`` were armed but confirmed still alive
    (slow, handle pinned); ``faults`` are the owed residue (a reclaim inverse
    that raised, or a confirmed-gone holder with no bound inverse). Shaped like
    `recovery.py`'s residue proofs: ``clean`` is true exactly when nothing is
    owed, and ``residue()`` renders the audit surface's ``reclaim`` record."""

    fired: list[str] = field(default_factory=list)
    pinned: list[str] = field(default_factory=list)
    faults: list[dict] = field(default_factory=list)
    at: float = 0.0

    @property
    def clean(self) -> bool:
        return not self.faults

    def residue(self) -> dict:
        """The `reclaim` residue record `revl audit` surfaces — a lease-lapse
        close is not a `bracket-fault`, so it reports as its own kind."""
        return {
            "record": "reclaim",
            "clean": self.clean,
            "fired": list(self.fired),
            "pinned": list(self.pinned),
            "outstanding": [f["holder"] for f in self.faults],
            "faults": list(self.faults),
            "proof": self._proof(),
        }

    def _proof(self) -> str:
        parts = [
            f"{len(self.fired)} holder(s) confirmed gone and their bound inverse "
            f"fired exactly once (out-of-frame reclaim, item 308 S1)"]
        if self.pinned:
            parts.append(
                f"{len(self.pinned)} armed holder(s) confirmed still alive were "
                f"PINNED (a slow holder pins its handle; the correct bias)")
        if self.faults:
            parts.append(
                f"{len(self.faults)} reclaim(s) are owed residue and did not "
                f"cleanly close — handed to `revl recover`/audit, not lost")
        else:
            parts.append("no residue: every confirmed-gone reclaim closed cleanly")
        return "; ".join(parts) + "."


# ------------------------------------------------------- shared crash-path

# Item 308 S1, the `shared` ownership mode's crash path, built on the primitive
# above. A `shared` acquire mints a COUNTED grant: N holders, one declared
# inverse bound to the count's zero crossing. Two teardown paths:
#
#   * ORDERLY — a holder `release`s; the last release (count -> 0) runs the
#     inverse as an ordinary bracket in that releaser's own LIFO stack. G5/G7
#     unchanged, `bracket-fault` on failure. This book returns the inverse for
#     the releaser to run; it does not fire it out of frame.
#   * CRASH — a holder is SIGKILLed without releasing. The liveness registry's
#     probe-gated reclaim confirms it gone and decrements on its behalf; if that
#     drives the count to zero with no live holder left to run the orderly
#     bracket, the reclaim executor fires the inverse out of frame and reports a
#     `reclaim` residue record. A hung but ALIVE holder pins the handle.


@dataclass
class SharedGrant:
    """A `shared`-mode resource: one handle, one declared inverse, N counted
    holders. The count is the 294-lease binding the design recommends over a
    bespoke refcount: holders are consumed as they release, and the zero
    crossing owns the inverse."""

    handle: str
    inverse: Inverse
    holders: set[str] = field(default_factory=set)
    fired: bool = False  # the count's zero-crossing fence (orderly or reclaim)


class SharedGrantBook:
    """The `shared` ownership mode's runtime, item 308 S1.

    Binds a :class:`SharedGrant`'s holder count to a :class:`LivenessRegistry`
    so the crash path is liveness-gated. Self-contained bookkeeping: the orderly
    zero crossing hands the inverse BACK to the last releaser (to run in its LIFO
    bracket); the crash zero crossing fires it out of frame via the registry's
    reclaim and reports it as `reclaim` residue."""

    def __init__(self, *, ttl: float = 30.0) -> None:
        self._grants: dict[str, SharedGrant] = {}
        self._liveness = LivenessRegistry(ttl=ttl)

    @property
    def liveness(self) -> LivenessRegistry:
        return self._liveness

    def acquire(self, handle: str, inverse: Inverse, holder_id: str, *,
                now: float | None = None) -> SharedGrant:
        """Mint (or join) a shared grant. The FIRST acquirer binds the handle's
        declared inverse to the zero crossing; every acquirer registers as a
        liveness holder and is counted."""
        grant = self._grants.get(handle)
        if grant is None:
            grant = SharedGrant(handle=handle, inverse=inverse)
            self._grants[handle] = grant
        if grant.fired:
            raise LivenessError(
                f"shared handle `{handle}` was already torn down at its zero "
                f"crossing; a new hold cannot join a closed grant (item 308 S1)")
        grant.holders.add(holder_id)
        self._liveness.register(holder_id, now=now)
        return grant

    def beat(self, holder_id: str, *, now: float | None = None) -> None:
        """A holder's heartbeat, forwarded to the liveness registry — the signal
        that pushes its deadline out and disarms a premature reclaim."""
        self._liveness.beat(holder_id, now=now)

    def handoff_ok(self, handle: str, peer_id: str, *,
                   probe: LivenessProbe | None = None,
                   now: float | None = None) -> bool:
        """Peer-alive gate for a `shared` handoff: may a holder pass this handle
        to / rely on ``peer_id``? Only while the peer is a counted holder AND
        confirmed alive. A handoff to a peer the runtime cannot confirm alive is
        refused so the handle is never seated in a dead frame."""
        grant = self._grants.get(handle)
        if grant is None or grant.fired or peer_id not in grant.holders:
            return False
        return self._liveness.confirmed_alive(peer_id, probe=probe, now=now)

    def release(self, handle: str, holder_id: str, *,
                now: float | None = None) -> Optional[Inverse]:
        """An orderly release. Decrements the count; returns the bound inverse
        (for the caller to run in its own LIFO bracket) exactly when this release
        is the zero crossing, else ``None``. Fences the inverse: a later release
        or a crash reclaim on the same grant fires nothing."""
        grant = self._grants.get(handle)
        if grant is None:
            raise LivenessError(
                f"no shared grant on handle `{handle}` to release (item 308 S1)")
        if holder_id not in grant.holders:
            raise LivenessError(
                f"holder `{holder_id}` does not hold shared handle `{handle}` "
                f"(item 308 S1)")
        grant.holders.discard(holder_id)
        # Only let the holder go from the liveness registry once it no longer
        # participates in ANY grant. Liveness identity is per holder (per
        # process), shared across every handle the holder owns, so releasing one
        # handle must not mark the holder released while it still holds another —
        # that would block the remaining handle's normal crash reclaim.
        if not self._holds_any(holder_id):
            self._liveness.release(holder_id, now=now)
        if grant.holders or grant.fired:
            return None
        grant.fired = True  # zero crossing, orderly path
        return grant.inverse

    def _holds_any(self, holder_id: str) -> bool:
        """Does ``holder_id`` still hold at least one (un-torn-down) grant?"""
        return any(holder_id in grant.holders
                   for grant in self._grants.values())

    def reclaim_crashed(self, *, probe: LivenessProbe,
                        now: float | None = None) -> ReclaimReport:
        """The crash path. For every holder the ``probe`` confirms gone, decrement
        its grant; a grant whose count reaches zero with no live holder left fires
        its inverse out of frame, exactly once, as a `reclaim` residue record.

        A grant still holding one or more live holders after the dead ones are
        removed is NOT torn down — a surviving holder still owns it, and the
        orderly path will run the inverse when that holder releases.

        Each zero-crossing handle runs its OWN declared inverse, independently
        and exactly once (fenced by the grant). A holder that owns several
        handles settles each one on its own: closing one handle — or failing to
        close it — neither skips nor settles any other handle the same holder
        owned. The holder's per-process liveness fence still fires at most once
        no matter how many of its handles cross zero."""
        now = time.time() if now is None else now
        self._liveness.arm_expired(now)

        def confirmed_gone(h: str) -> bool:
            holder = self._liveness.holder(h)
            return (holder is not None and holder.state == _ARMED
                    and not probe.alive(h))

        # Fire each zero-crossing handle's own inverse here, per grant, fenced by
        # `grant.fired`. Firing per handle (each in its own frame) is what keeps
        # a holder's several handles independent: the registry reclaim below only
        # settles the holder's per-process liveness, and it fires a no-op close
        # since the real close already ran here.
        noop: Inverse = lambda: None
        gone: dict[str, Inverse] = {}
        handle_faults: list[dict] = []
        for grant in self._grants.values():
            if grant.fired:
                continue
            dead = sorted(h for h in grant.holders if confirmed_gone(h))
            if not dead:
                continue
            # every confirmed-gone holder is reclaimed from this grant's count;
            # its per-process liveness fence still fires at most once.
            for h in dead:
                gone[h] = noop
            if [h for h in grant.holders if h not in dead]:
                # a live holder still owns the handle: drop the dead holders from
                # the count but leave the grant standing (no close).
                continue
            # zero crossing with no live holder left: this handle owns its close.
            carrier = dead[0]
            grant.fired = True  # per-handle fence: orderly or reclaim, once
            try:
                grant.inverse()
            except Exception as error:  # noqa: BLE001 — recorded as residue
                handle_faults.append({
                    "holder": carrier,
                    "handle": grant.handle,
                    "kind": "reclaim-fault",
                    "detail": (f"the reclaim inverse for shared handle "
                               f"`{grant.handle}` (carried by gone holder "
                               f"`{carrier}`) raised: {error}")})

        # Settle every confirmed-gone holder through the registry's exactly-once
        # reclaim fence (a no-op close — the handle's real inverse already fired
        # above). This yields the per-holder fired/pinned accounting and the
        # event trace, and fences a holder so a re-run reclaims it no second time.
        report = self._liveness.reclaim(gone, probe=probe, now=now)

        # A handle whose own inverse raised is owed residue: move its carrier out
        # of the clean-fired set and record the per-handle fault, so a holder's
        # clean per-process reclaim never papers over a handle that did not close.
        if handle_faults:
            faulted = {f["holder"] for f in handle_faults}
            report.fired = [h for h in report.fired if h not in faulted]
            report.faults.extend(handle_faults)

        # drop the holders the reclaim consumed from every grant's count.
        for grant in self._grants.values():
            grant.holders = {h for h in grant.holders
                             if self._liveness.holder(h) is None
                             or self._liveness.holder(h).state != _RECLAIMED}
        return report

    # -- reads -------------------------------------------------------------

    def grant(self, handle: str) -> SharedGrant | None:
        return self._grants.get(handle)

    def count(self, handle: str) -> int:
        grant = self._grants.get(handle)
        return len(grant.holders) if grant is not None else 0

"""Host-runtime adapter and stub stdlib for the revl cordis-py backend.

This module is everything an emitted component imports.  It has two halves:

* **Adapter** — :class:`Frame`, the per-activation accumulator that maps the
  IR's component-level LIFO recovery (R1) onto cordis-py's effect protocol.
  cordis-py unloads a fiber's *top-level* effects concurrently (its
  `asyncio.gather` mirrors the upstream `Promise.all`) and only guarantees
  LIFO order *within* a single effect's yielded disposers.  The emitter
  therefore compiles a component body to exactly one `ctx.effect(generator)`
  whose yields are the accumulated inverses, and `Frame` adopts any effects
  registered later by provide-method calls so they are drained — newest
  first — ahead of the activation-time inverses.

* **Host stdlib** — the `Pool` / `Map` / `Job` host builtins required by
  docs/backend-ir.md.  `Map` is a real in-memory map; `Pool` is a real
  bounded connection pool over a deterministic fake database; `Job` is a
  real cancellable asynchronous unit of work.  All three record every
  operation so demos and tests can assert ordering.

  This module is the **reference definition** of the Pool/Job semantics — see
  ":ref:`pool-job-semantics`" below.  The cordis (TS), cordis-rs and cordis4j
  tiers implement the same state machines and point back here; a tier that
  drifts from this text is a bug in that tier, not a local dialect.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import time
import weakref
from typing import Any, Callable, Optional

__all__ = [
    "Clock", "ConfigError", "ConfigSchema", "FaultProbe", "Frame", "Job", "JobCancelled",
    "JobHandle", "Map", "Pool", "PoolError", "SessionCommitError", "SessionOwner",
    "SpawnHandle", "StateIncompatible",
    "TimerHandle", "TransientError", "add_trace", "arm_fault_probe", "clear_session_owner",
    "disarm_fault_probe",
    "fmt", "live_instances", "plug", "realm_label", "remove_trace", "resolved_config",
    "retry_idempotent", "schedule_after", "schedule_every", "session_owner",
    "set_session_owner", "set_trace", "spawn",
    "trace_observers",
]


# ---------------------------------------------------------------------------
# Delivery semantics (docs/delivery-semantics.md, roadmap item 44)
# ---------------------------------------------------------------------------
#
# The IR promotes idempotency to a checked property: `emission idempotent fn`.
# Only that promise earns the runtime a new right — to *auto-retry* the
# emission on a transient failure, because re-delivering it is defined to have
# the same effect as delivering it once (`f(f(x)) == f(x)` on the server).
#
# A host signals "this failure was transient — the emission did not durably
# land, so re-issuing it is well-defined" by raising `TransientError`. Any
# other exception is a real error and is never retried. And a `TransientError`
# from a *non*-idempotent emission is still never retried: without the checked
# property, a second delivery could double the effect, so the runtime has no
# right to it. This is the whole point of making idempotency a checked flag.

class TransientError(RuntimeError):
    """A host-signalled transient delivery failure of an emission.

    Raising this from an emission's host body tells the runtime the emission
    did not durably land (a dropped connection, a 503, a timeout before the
    server committed). The runtime retries it *iff* the emission is declared
    `idempotent`; for any other emission it propagates unchanged.
    """


async def retry_idempotent(call, *, idempotent: bool = False,
                           attempts: int = 3, where: str = ""):
    """Invoke ``call`` and, for an idempotent emission, auto-retry a transient
    failure up to ``attempts`` times.

    ``call`` is a zero-argument callable that performs one delivery; its result
    is awaited if awaitable. A :class:`TransientError` is retried only when
    ``idempotent`` is true — a non-idempotent emission gets exactly one attempt
    and the transient failure propagates. Every non-transient exception
    propagates immediately, retries or not.
    """
    budget = attempts if idempotent else 1
    last: TransientError | None = None
    for _ in range(budget):
        try:
            result = call()
            if inspect.isawaitable(result):
                result = await result
            return result
        except TransientError as exc:  # noqa: PERF203 — retry is the point
            last = exc
    # budget exhausted (or a single non-idempotent attempt): re-raise the
    # transient failure so the caller still sees a failed delivery
    raise last


# ---------------------------------------------------------------------------
# v2: realm placement (docs/design-v2-realms.md)
# ---------------------------------------------------------------------------

class _RealmLabel:
    """A realm identity. cordis compares isolate labels by object identity,
    so same-string sharing must go through one object — never rely on
    string interning."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<realm {self.name}>"


_REALM_LABELS: dict = {}


def realm_label(name: str) -> "_RealmLabel":
    """Process-wide string -> label-object registry: equal strings share a
    realm (the paper §5.2.1 global-realm convention)."""
    label = _REALM_LABELS.get(name)
    if label is None:
        label = _REALM_LABELS[name] = _RealmLabel(name)
    return label


def plug(ctx, component: dict, config=None):
    """Load an emitted component honoring its realm placements: apply
    ctx.isolate(key, label) per entry BEFORE ctx.plugin — the fiber's
    context chain is fixed at plugin time, so isolation cannot happen
    inside apply."""
    scoped = ctx
    for key, realm in (component.get("isolate") or {}).items():
        scoped = scoped.isolate(key, realm_label(realm))
    return scoped.plugin(component, config)


# ---------------------------------------------------------------------------
# instance-parametric components (docs/design-v2-instances.md)
# ---------------------------------------------------------------------------

# -- live-instance state migration (roadmap item 10, hot-swap with instances) -
#
# Hot-swapping a template `T` to `T'` while instances of `T` are live must
# reconcile each live instance's *state* onto `T'` (docs/design-v2-instances.md
# "Not in phase 1", question 6). The swap engine (src/revl/mcp/session.py) drives
# the reconciliation; this section is the runtime substrate it stands on:
#
#   * a **live-instance registry** so the engine can enumerate the live
#     instances of a swapped template (`live_instances(name)`);
#   * per-activation **resource tracking** so an instance's migratable state —
#     the stateful host resources it acquired (its `Map`, …) — can be captured
#     and restored (`SpawnHandle.capture_state` / `.restore_state`).
#
# All of it is inert unless something spawns: no spawn ⇒ no registry entries, and
# the activation hook only records resources while a body is actually running.

#: fibers whose body is *currently* activating — a stack, innermost last. A host
#: resource created inside a body (`Map.new()`) registers onto the top frame, so
#: it is attributed to the activation (hence the instance) that acquired it.
_ACTIVATING: list["Frame"] = []

#: ctx -> the Frame of the activation on that context. Weak-keyed so a torn-down
#: instance's frame is collected with its context. A `SpawnHandle` finds its
#: instance's frame here, via the fiber context it already holds.
_FRAME_BY_CTX: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

#: component name -> the live `SpawnHandle`s of that template, in spawn order.
#: A list (not a set): the swap engine correlates old instances to the new ones
#: the successor template spawns *positionally*, and spawn order is deterministic.
_LIVE_INSTANCES: "dict[str, list[SpawnHandle]]" = {}


def _register_resource(resource: Any) -> None:
    """Attribute a freshly-created host resource to the activation acquiring it.

    Called from `_Closable.__init__`; a no-op when nothing is activating (a
    resource made outside any component body — e.g. a bare test — is not
    instance state and is simply not tracked)."""
    if _ACTIVATING:
        _ACTIVATING[-1]._resources.append(resource)


def _frame_for_ctx(ctx: Any) -> "Optional[Frame]":
    try:
        return _FRAME_BY_CTX.get(ctx)
    except TypeError:  # pragma: no cover — non-weakrefable ctx
        return None


def live_instances(component: str) -> "list[SpawnHandle]":
    """The live instances of a template, in spawn order (the swap engine's
    enumeration point). A copy, so disposal during iteration is safe."""
    return list(_LIVE_INSTANCES.get(component, ()))


def _remember_instance(handle: "SpawnHandle") -> None:
    _LIVE_INSTANCES.setdefault(handle.component, []).append(handle)


def _forget_instance(handle: "SpawnHandle") -> None:
    bucket = _LIVE_INSTANCES.get(handle.component)
    if bucket is not None:
        try:
            bucket.remove(handle)
        except ValueError:
            pass
        if not bucket:
            _LIVE_INSTANCES.pop(handle.component, None)


class StateIncompatible(RuntimeError):
    """A live instance's captured state cannot migrate onto the successor
    template — the successor dropped or retyped a resource the instance held.
    Raised by `SpawnHandle.restore_state`; the swap engine turns it into an
    admission-style rejection and rolls back rather than dropping the state."""


class SpawnHandle:
    """The value a `spawn` acquisition binds: a live component instance, torn
    down by its own `.dispose()`.

    The instance is a child fiber of its spawner — its own nested teardown
    scope (item zero). `dispose()` unloads that fiber, running the instance's
    LIFO teardown *now*, independent of the spawner: a request-scoped instance
    is reclaimed when the request ends, never deferred to the component's
    teardown. Disposal is idempotent, so the spawner's own inverse
    (`yield lambda: s.dispose()`, or the frame-adopted safety net for a
    method-body spawn) is a harmless no-op once the instance is already gone."""

    __slots__ = ("_fiber", "component", "_disposed", "__weakref__")

    def __init__(self, fiber, component: str) -> None:
        self._fiber = fiber
        self.component = component
        self._disposed = False

    def dispose(self):
        """Unload the instance's fiber (its LIFO teardown). Returns the
        runtime's awaitable so a caller in an async context can `await` the
        reclamation; the emitted inverse is drained through the effect
        protocol, which already awaits an awaitable disposer."""
        if self._disposed:
            return None
        self._disposed = True
        _forget_instance(self)
        return self._fiber.dispose()

    # -- state migration (hot-swap with live instances) --------------------

    def _frame(self) -> "Optional[Frame]":
        """The instance's activation frame, which tracks the stateful host
        resources it acquired. `None` if the instance never activated (its
        provisions never came up) — nothing to migrate."""
        return _frame_for_ctx(self._fiber.ctx)

    def capture_state(self) -> list:
        """Snapshot this live instance's migratable state: the ordered vector
        of `(resource_type, state)` for each stateful host resource the
        instance acquired during activation. `state` is `None` for a resource
        with no `__revl_state__` (e.g. a `Pool`, whose checkout bookkeeping is
        transient) — its *type* is still pinned so the compat gate can require
        the successor to acquire an equivalent one.

        The vector's order is acquisition order (source order of the
        instance's `let-effect` steps), which the successor reproduces
        deterministically — that is what makes the positional restore sound."""
        frame = self._frame()
        resources = list(frame._resources) if frame is not None else []
        captured = []
        for res in resources:
            snap = res.__revl_state__() if hasattr(res, "__revl_state__") else None
            captured.append((type(res), snap))
        return captured

    def check_state(self, captured: list) -> None:
        """The state-compat gate, without mutating anything: raise
        `StateIncompatible` if a `capture_state()` vector cannot migrate onto
        this (fresh) instance, else return.

        The successor must have acquired a resource vector of the *same length*
        and, at each position, of the *same type*. Any divergence — a dropped
        resource, a retyped one, a changed count — means the successor cannot
        hold the predecessor's state, and silently losing it would be residue.
        Kept separate from `restore_state` so a whole cohort can be *checked*
        before any of it is *applied* — that is what makes a rejected migration
        roll back with nothing half-written."""
        resources = self._resource_vector()
        if len(resources) != len(captured):
            raise StateIncompatible(
                f"instance of {self.component!r} held {len(captured)} stateful "
                f"resource(s); the successor acquires {len(resources)} — state "
                f"cannot migrate without dropping or inventing a resource")
        for pos, (res, (old_type, _snap)) in enumerate(zip(resources, captured)):
            if type(res) is not old_type:
                raise StateIncompatible(
                    f"instance of {self.component!r}: resource #{pos} was "
                    f"{old_type.__name__}, the successor acquires "
                    f"{type(res).__name__} — retyped state cannot migrate")

    def restore_state(self, captured: list) -> None:
        """Migrate a checked `capture_state()` vector onto this instance: write
        each captured state back through its resource's `__revl_restore__`.
        Re-runs `check_state` so a direct caller is still gated."""
        self.check_state(captured)
        resources = self._resource_vector()
        for res, (_old_type, snap) in zip(resources, captured):
            if snap is not None:
                res.__revl_restore__(snap)

    def _resource_vector(self) -> list:
        frame = self._frame()
        return list(frame._resources) if frame is not None else []

    def get(self, key: str):
        """Read a provision the instance published, in *its* local realm.
        Only the spawner (which holds this handle) can reach it — a sibling
        instance, isolated into a different local realm, cannot (supervision-
        tree addressing, decision 1/2)."""
        return self._fiber.ctx.get(key)


def spawn(ctx, component: dict, config, realms):
    """Instantiate `component` at runtime as a child of the spawner, each key
    it provides isolated into a *fresh* LOCAL realm (unlabeled `ctx.isolate`,
    which mints a distinct identity per call). Two instances of one component
    therefore never collide on a provision — disjoint by construction, no
    config value known at link time (decision 3/5). Returns a
    :class:`SpawnHandle`."""
    scoped = ctx
    for key in realms or ():
        scoped = scoped.isolate(key)  # no label -> a fresh local realm per spawn
    fiber = scoped.plugin(component, dict(config or {}))
    handle = SpawnHandle(fiber, component.get("name"))
    _remember_instance(handle)  # so a hot-swap can enumerate live instances
    return handle


# ---------------------------------------------------------------------------
# tracing (test/demo observability for the stub stdlib)
# ---------------------------------------------------------------------------

_trace: Optional[Callable[[str], None]] = None
_observers: list = []


def set_trace(callback: Optional[Callable[[str], None]]) -> None:
    """Install *the primary* trace callback (the original single-observer API).

    Replaces whatever a previous ``set_trace`` installed; ``set_trace(None)``
    clears it.  Observers registered through :func:`add_trace` live in a
    separate list and are **not** disturbed by this call, so a demo can hold
    its own subscription across a driver's install/teardown of the primary
    callback.
    """
    global _trace
    _trace = callback


def add_trace(callback: Callable[[str], None]) -> Callable[[], None]:
    """Subscribe an *additional* trace observer; returns its unsubscriber.

    Any number of observers may coexist.  Each event goes to the primary
    ``set_trace`` callback first (if any), then to every observer in
    subscription order.  The returned function is idempotent.
    """
    _observers.append(callback)
    state = {"live": True}

    def unsubscribe() -> None:
        if state["live"]:
            state["live"] = False
            try:
                _observers.remove(callback)
            except ValueError:  # already removed via remove_trace
                pass

    return unsubscribe


def remove_trace(callback: Callable[[str], None]) -> bool:
    """Unsubscribe an observer added with :func:`add_trace`."""
    try:
        _observers.remove(callback)
    except ValueError:
        return False
    return True


def trace_observers() -> int:
    """How many callbacks are currently receiving events (primary included)."""
    return len(_observers) + (1 if _trace is not None else 0)


def _record(event: str) -> None:
    if _trace is not None:
        _trace(event)
    for observer in list(_observers):
        observer(event)


# ---------------------------------------------------------------------------
# fault probe: instrumentation for `fault test` (docs/fault-tests.md)
# ---------------------------------------------------------------------------


class FaultProbe:
    """Records one component activation's inverse accumulation and replay.

    A `fault test` needs two orders to compare: the order in which the
    activation *accumulated* its inverses, and the order in which the runtime
    actually *ran* them while unwinding.  Both are observed here rather than
    reconstructed from the host trace, because a host trace only sees effects
    that touch a stub builtin — a `provide` withdrawal or a pure closure undo
    leaves no trace event at all, and those are exactly the inverses an
    L-Raise regression would drop.

    The probe wraps the component body generator installed by
    :meth:`Frame.install` and tags every yielded disposer with its
    accumulation index; the wrapper appends that index to :attr:`ran` when the
    runtime disposes it.  R1/A8 hold iff ``ran == list(reversed(accumulated))``.

    Only the component named at arming time is instrumented, so siblings in
    the same composition run exactly as they normally would.
    """

    __slots__ = ("component", "accumulated", "ran", "_n")

    def __init__(self, component: str) -> None:
        self.component = component
        self.accumulated: list = []   # accumulation indices, in order
        self.ran: list = []           # the same indices, in disposal order
        self._n = 0

    # -- instrumentation ---------------------------------------------------

    def _tag(self, value: Any, frame: "Frame") -> Any:
        """Wrap one yielded disposer; pass anything else through untouched."""
        if not callable(value):
            return value            # `yield None` — an A1 iteration boundary
        if getattr(value, "__self__", None) is frame:
            return value            # `yield frame.drain` — the accumulator itself
        self._n += 1
        index = self._n
        self.accumulated.append(index)

        def _instrumented(_index=index, _undo=value):
            self.ran.append(_index)
            return _undo()

        return _instrumented

    def instrument(self, body: Callable, frame: "Frame") -> Callable:
        """Return a body generator function equivalent to *body*, tagged.

        cordis iterates an effect body with a plain ``for``/``async for`` and
        never ``send``s or ``throw``s into it (see cordis fiber ``_execute``),
        so a re-yielding wrapper is protocol-faithful.
        """
        if inspect.isasyncgenfunction(body):
            async def _async_wrapper():
                async for value in body():
                    yield self._tag(value, frame)
            return _async_wrapper

        def _wrapper():
            for value in body():
                yield self._tag(value, frame)
        return _wrapper

    # -- results -----------------------------------------------------------

    def lifo_violation(self) -> Optional[tuple]:
        """``(position, ran_index, expected_index)`` for the first inverse that
        broke LIFO, or ``None`` when the replay was exact.

        ``ran_index`` is ``None`` when the replay simply stopped early — an
        inverse that never ran is a LIFO violation too, and the more serious
        one, so it is reported here rather than silently passing.
        """
        expected = list(reversed(self.accumulated))
        for position, index in enumerate(self.ran):
            if position >= len(expected):
                return (position + 1, index, None)
            if expected[position] != index:
                return (position + 1, index, expected[position])
        if len(self.ran) != len(expected):
            return (len(self.ran) + 1, None, expected[len(self.ran)])
        return None

    def never_ran(self) -> list:
        """Accumulation indices whose inverse was never disposed."""
        ran = set(self.ran)
        return [index for index in self.accumulated if index not in ran]


_fault_probe: Optional[FaultProbe] = None


def arm_fault_probe(component: str) -> FaultProbe:
    """Instrument the next activation of *component*; returns the probe.

    Process-global and single-slot on purpose: a fault test drives exactly one
    activation at a time, and a leftover probe from a crashed run is visible
    rather than silently accumulating.
    """
    global _fault_probe
    _fault_probe = FaultProbe(component)
    return _fault_probe


def disarm_fault_probe() -> None:
    global _fault_probe
    _fault_probe = None


# ---------------------------------------------------------------------------
# adapter: the component accumulator
# ---------------------------------------------------------------------------


def _named_call_method(undo: Callable[[Any], Any]) -> str:
    """Best-effort inverse name for a WAL discharge-descriptor's `call.method`.

    243's grammar declares `undo` as an ordinary pure-expression call (docs/
    design/243-witnessed-externs.md, note 1): `_witnessed_step` in emit.py
    compiles the call site to `lambda result: <declared undo>(...)`, so the
    lambda's own bytecode references the declared inverse's name as a global
    lookup — `co_names[0]` recovers it without needing the call-site metadata
    the (separate, parallel) lower.py enablement slice will eventually thread
    through. Falls back to the callable's own `__name__` (or a fixed label)
    for anything that is not a plain generated lambda, e.g. a hand-built test
    fixture — never raises, this is best-effort naming for the descriptor,
    not the replay path itself."""
    code = getattr(undo, "__code__", None)
    if code is not None and code.co_names:
        return code.co_names[0]
    return getattr(undo, "__name__", None) or "undo"


class _Transactional:
    """The disposer for one witnessed (transactional) effect (item 243).

    A bracket disposer replays its inverse on every teardown — clean unload and
    abort alike — because releasing an acquired handle is always right. A
    transactional disposer is different: the mutation IS the deliverable, so on
    a clean COMMIT (the activation completed and is now unloading cleanly) the
    inverse must NOT run and the witness is dropped; only an ABORT (a
    mid-activation failure that unwinds before the activation ever committed)
    replays it. Both branches drop the inverse and witness references so a
    discharged transaction leaves no rollback state alive (witness GC).

    The commit signal is read from the owning frame at disposal time, not
    captured at registration, because whether the activation commits is not yet
    known when the effect runs — it depends on whether a LATER step aborts."""

    __slots__ = ("frame", "witness", "_undo", "discharged", "replayed", "seq",
                 "_escrowed")

    def __init__(self, frame: "Frame", undo: Callable[[Any], Any], witness: Any) -> None:
        self.frame = frame
        self._undo = undo
        self.witness = witness
        self.discharged = False   # committed: inverse skipped, mutation persists
        self.replayed = False     # aborted: inverse ran, mutation reverted
        # the WAL discharge-descriptor's `seq` this entry was registered under
        # (bridge slice: connects Slice 2a to the WAL/recover foundation), or
        # `None` when no WriteAheadLog is attached (a plain run) — see
        # `Frame._wal`/`Frame.transactional`.
        self.seq: Optional[int] = None
        # item 245: escrowed under a session owner whose verdict is still
        # pending (a mid-session withdrawal). Held once — neither discharged nor
        # replayed — until the owner settles the verdict and disposes it.
        self._escrowed = False

    def __call__(self) -> Any:
        if _hold_for_session(self):
            return None
        if self.frame._committed:
            # commit: discharge. The mutation stays; drop the inverse + witness
            # so no rollback state survives a committed transaction (GC).
            self.discharged = True
            self._undo = None
            self.witness = None
            return None
        # abort: replay the declared inverse against the captured witness, then
        # drop both references (idempotent single-shot, so a re-entrant unwind
        # or a `revl recover` pass does not run it twice).
        self.replayed = True
        undo, self._undo = self._undo, None
        witness, self.witness = self.witness, None
        if undo is None:  # pragma: no cover — single-flight guard
            return None
        return undo(witness)


#: Phase-2 bound config surface (docs/design/teardown-contract.md, "the
#: bound rule"): two environment variables in the existing `REVL_*`
#: convention, identical spelling on every tier, read once per activation
#: (`Frame.__init__`) so a change mid-run cannot move an already-aborting
#: activation's deadline. Provisional defaults pinned by the contract
#: (open question 1): budget 5000 ms, per-call 1000 ms.
_COMPENSATION_BUDGET_ENV = "REVL_COMPENSATION_BUDGET_MS"
_COMPENSATION_PER_CALL_ENV = "REVL_COMPENSATION_PER_CALL_MS"
_COMPENSATION_BUDGET_DEFAULT_MS = 5000
_COMPENSATION_PER_CALL_DEFAULT_MS = 1000


def _read_bound_seconds(env_name: str, default_ms: int) -> Optional[float]:
    """Read one `REVL_*_MS` bound: unset -> the tier-uniform default; `0` ->
    no bound (`None`, teardown-contract.md: "the between-compensation check
    still runs and records nothing expired"); an unparsable value -> the
    default, same as unset (never crash an activation over a malformed
    host env var). Returns seconds, `time.monotonic`'s unit."""
    raw = os.environ.get(env_name)
    if raw is None:
        ms = default_ms
    else:
        try:
            ms = int(raw)
        except ValueError:
            ms = default_ms
    if ms == 0:
        return None
    return ms / 1000.0


def _residue_record(entry: "_Compensation", *, outcome: str,
                    attempted_flag: bool, attempted: Optional[dict],
                    error: dict) -> dict:
    """One `compensation-residue` fact (item 247 gap 2, design Decision 2).

    A best-effort offset that raised (`failed`) or never got to run under the
    Phase-2 budget (`not-attempted`) is residue: a crossing whose compensation
    was OWED but did not land. Every such record carries `state: "unresolved"`
    — the third audit state joining `bare`/`compensated` — and NAMES the
    crossing it was offsetting (`component`, `method`, WAL `seq`), so the audit
    surface (`revl.query`/`revl.erase_report`) and the 246 session-boundary
    report can enumerate it rather than let an in-memory list silently grow."""
    return {
        "kind": "compensation-residue",
        "state": "unresolved",
        "component": entry.component,
        "method": entry.method,
        "seq": entry.seq,
        "attemptedFlag": attempted_flag,
        "attempted": attempted,
        "outcome": outcome,
        "error": error,
    }


class _Compensation:
    """The disposer for one compensation entry (item 247, docs/design/
    teardown-contract.md 'the three entry kinds, one stack'). Registered at
    an `emit ... compensate ...` step, it joins the SAME per-activation LIFO
    disposer stack as bracket and transactional entries — there is no second
    list (the contract's decision 1: the distinction lives in the entry, not
    in a separate structure).

    A compensation offsets an EMISSION that already crossed the boundary
    (mail sent, message published); it cannot be reversed, only offset. Like
    a transactional entry, a clean COMMIT discharges it — it never runs,
    because the emission itself is the deliverable and best-effort cleanup
    on success would be wrong. Unlike a transactional entry, an ABORT does
    NOT run it inline during the Phase-1 unwind: interleaved with the proof
    inverses in one pass, a raising compensation could leave a LATER (older)
    bracket/transactional inverse un-run, which is strictly worse residue
    (teardown-contract.md, "why two phases", reason 1). So `__call__` — the
    Phase-1 encounter, invoked by cordis's own LIFO disposal — never invokes
    `fn`. It only ENQUEUES this entry on the owning frame's
    `_pending_compensations`, in the exact order cordis's reverse-registration
    unwind encounters it (already the correct Phase-2 LIFO order — newest
    compensation first), and returns immediately, a Phase-1 no-op.

    `Frame.install`'s post-unwind hook drains that queue — actually invoking
    `fn` — only after the WHOLE Phase-1 pass has finished, via
    `_run_phase2`. This is the "compensation disposer enqueues itself ...
    and drains the queue in a post-unwind hook" mechanism teardown-
    contract.md names as natural on a cordis tier, where the runtime unwinds
    every disposer in one synchronous stack-position pass."""

    __slots__ = ("frame", "fn", "discharged", "ran", "failed", "error", "seq",
                 "_escrowed", "component", "method")

    def __init__(self, frame: "Frame", fn: Callable[[], Any],
                 method: Optional[str] = None) -> None:
        self.frame = frame
        self.fn = fn
        self.discharged = False   # committed: never runs, deliverable persists
        self.ran = False          # Phase 2: actually invoked (any outcome)
        self.failed = False       # Phase 2: invoked and raised (continue-and-record)
        self.error: Optional[BaseException] = None
        # item 247 gap 2: the offset emission's identity, so a residue record
        # NAMES the crossing it was offsetting on the audit surface (design
        # Decision 2: "the record names the original emission it was offsetting").
        # `component` is the activation the compensation belongs to; `method` is
        # the offsetting call the emitter wrote (from `_named_call_method`).
        self.component: Optional[str] = frame.name
        self.method: Optional[str] = method
        # the WAL discharge-descriptor's `seq` this entry was registered
        # under (mirrors `_Transactional.seq`), or `None` when no
        # WriteAheadLog is attached.
        self.seq: Optional[int] = None
        # item 245: escrowed under a session owner with a pending verdict.
        self._escrowed = False

    def __call__(self) -> Any:
        if _hold_for_session(self):
            return None
        if self.frame._committed:
            # commit: discharge. The emission was the deliverable; a
            # best-effort offset on success would be wrong to run.
            self.discharged = True
            self.fn = None
            return None
        # abort, Phase-1 encounter: defer to Phase 2 — do not invoke `fn`
        # here. Enqueueing (rather than running immediately) is what keeps a
        # failing compensation from ever interrupting proof replay.
        self.frame._pending_compensations.append(self)
        return None

    def _run_phase2(self) -> None:
        """Phase 2 only: actually invoke the compensation. Best-effort — it
        catches and records rather than raising, so one failed offset never
        fails the abort and never blocks the remaining ones (teardown-
        contract.md's continue-and-record rule, `compensation-residue`
        severity)."""
        self.ran = True
        fn, self.fn = self.fn, None
        if fn is None:  # pragma: no cover — single-flight guard
            return
        try:
            fn()
        except BaseException as error:  # noqa: BLE001 — anticipated, best-effort
            self.failed = True
            self.error = error


# ---------------------------------------------------------------------------
# item 245: the session commit protocol (docs/design/245-session-commit.md)
# ---------------------------------------------------------------------------
#
# A session is one driver lifetime = one WAL. The driver registers a
# `SessionOwner` around the session (`set_session_owner`), and every `Frame`
# built while one is registered joins its live-frame registry. The owner holds
# the three session-scoped structures the commit verb derives its GATE TARGET
# from — the deferral queue, the discharge escrow, the live-frame registry —
# never the runtime's per-call current frame (`_FRAME_BY_CTX`).
#
# The owner is process-global and single-slot, exactly like `_trace`: one driver
# runs a session at a time. When NO owner is registered (`_SESSION_OWNER is
# None`), every `Frame` behaves byte-identically to the pre-245 world — a clean
# drain discharges at unload (implicit commit), the compatibility clause.

_SESSION_OWNER: "Optional[SessionOwner]" = None


def set_session_owner(owner: "SessionOwner") -> None:
    """Register the session's commit-state owner for the frames built next.

    The driver calls this once at session start, before loading the
    composition, so each activation `Frame.__init__` joins the owner's registry.
    Idempotent-ish: replacing an owner mid-session is a caller error, not
    guarded here (one driver owns one session)."""
    global _SESSION_OWNER
    _SESSION_OWNER = owner


def clear_session_owner() -> None:
    """Unregister the session owner (the driver calls this at commit/abort/unload
    end). Frames built afterwards get the pre-245 implicit-commit semantics."""
    global _SESSION_OWNER
    _SESSION_OWNER = None


def session_owner() -> "Optional[SessionOwner]":
    return _SESSION_OWNER


def _hold_for_session(entry: Any) -> bool:
    """Whether a transactional/compensation entry must be HELD now rather than
    discharged or replayed (item 245's escrow). It holds iff its frame has a
    session owner whose verdict is still pending AND the frame is not aborting —
    i.e. this is a MID-SESSION withdrawal (a swap/undo), whose entries wait for
    the session verdict rather than committing at the withdrawal. A held entry
    escrows itself once so the owner disposes it when the verdict settles.

    A terminal teardown (the owner's own commit/abort/unload) sets the verdict
    BEFORE disposing, so nothing holds there — the per-frame `_committed` /
    `_aborting` bits govern discharge-vs-replay exactly as before. An aborting
    frame never holds: its inverses replay immediately."""
    frame = entry.frame
    owner = frame._owner
    if owner is None or owner._verdict is not None or frame._aborting:
        return False
    # `_holding` is the discriminator between the two ways a frame's inverses
    # can run with the verdict still pending. It is set ONLY by `drain`'s
    # mid-session-withdrawal branch (the activation completed and is being torn
    # down while the session continues), never by a mid-ACTIVATION failure
    # (the body raised before `drain` — the classic 243 abort, whose inverses
    # replay immediately). Without this a failed activation under a session
    # owner would wrongly escrow its inverse instead of reverting.
    if not frame._holding:
        return False
    if not entry._escrowed:
        entry._escrowed = True
        owner.escrow(entry)
    return True


class Frame:
    """One component activation's effect accumulator.

    The emitted ``apply`` creates a fresh ``Frame`` per activation and
    installs the component body as a single ``ctx.effect`` generator.  The
    generator's final ``yield frame.drain`` places the drain at the *top* of
    the runtime's LIFO disposer stack, so inverses accumulated after
    activation (provide-method ``effect`` steps, adopted below) are undone
    first, newest first, before the activation-time inverses run — exact
    component-level LIFO on a runtime that is only per-effect LIFO.

    Every inverse still lives in a genuine ``ctx.effect``; disposal is the
    runtime's single-flight ``FiberEffect``, so drain and the fiber's own
    unload pass can both reach a wrapper without ever double-freeing it.

    Empirically the fiber's unload already starts disposals newest-first
    (``DisposableList.clear()`` reverses), so with synchronous undos the
    drain usually finds the adopted effects disposed and no-ops.  That
    ordering is an implementation detail the cordis README explicitly
    disclaims ("LIFO ordering is preserved only within a single effect's
    yielded disposers"); the drain turns R1 from a property of that detail
    into a property of the documented per-effect contract.  See REPORT.md.
    """

    def __init__(self, ctx: Any, name: str) -> None:
        self.ctx = ctx
        self.name = name
        self._adopted: list = []
        # item 243 (docs/design/243-witnessed-externs.md): the transactional
        # entries this activation registered, in registration order. A witnessed
        # effect is NOT a bracket — its inverse replays on ABORT ONLY and is
        # DISCHARGED (skipped, witness GC'd) on a clean successful unload, where
        # the mutation itself is the deliverable and must persist. `_committed`
        # is the abort-vs-commit discriminator: it flips True the moment `drain`
        # runs, and `drain` runs iff the body reached its final `yield` (a clean
        # unload). A mid-activation abort never yields `drain`, so cordis unwinds
        # the already-collected disposers with `_committed` still False and the
        # transactional inverse replays exactly like a bracket. Kept for
        # introspection (fault/residue proofs); the disposer reads `_committed`.
        self._transactional: list = []
        self._committed: bool = False
        # item 318 (docs/design/243-witnessed-externs.md): the transactional
        # entries a PROVIDE-METHOD registered (a per-tool-call witnessed fs
        # mutation), as opposed to the activation body's own. A method body has
        # no body generator to `yield` a disposer into, and adopting it as a
        # sibling `ctx.effect` is UNSOUND: cordis disposes an adopted effect
        # BEFORE the body effect's final `yield _revl_frame.drain` runs, so the
        # disposer would observe `_committed` still False on a CLEAN unload and
        # wrongly replay (revert) the deliverable. So a method-registered
        # transactional entry is NOT a cordis disposer; it is parked here and
        # disposed by `drain` itself, once `_committed`/`_aborting` are settled,
        # so it observes the correct commit-vs-abort bit by construction. Each
        # entry is also appended to `_transactional` above so the WAL discharge
        # record (`drain`) and fault/residue introspection cover it uniformly.
        self._deferred_transactional: list = []
        # item 318: the abort discriminator for a component that already
        # activated cleanly. `_committed` (flipped by `drain`) answers "did the
        # ACTIVATION body complete"; a per-tool-call mutation runs AFTER
        # activation, so on any later teardown `drain` runs and would always
        # commit it. A session-level reject (item 245's explicit commit/abort
        # UX drives this) sets `_aborting` BEFORE teardown; `drain` then leaves
        # `_committed` False, so both the activation-body transactional entries
        # (cordis disposers reading `_committed`) and the method-registered
        # deferred entries replay and the mutations revert, residue-free.
        self._aborting: bool = False
        # item 247 (docs/design/teardown-contract.md): the compensation
        # entries this activation registered, in registration order — kept
        # for introspection, same role as `_transactional` above. The actual
        # Phase-2 queue is `_pending_compensations`, populated as `_Compensation`
        # disposers are encountered during an abort's Phase-1 unwind (see
        # `_Compensation.__call__`) and drained by `_drain_phase2` after
        # `install`'s `ctx.effect(...)` call re-raises the body's failure.
        self._compensations: list = []
        # item 247 (method-body compensate remainder) (docs/design/teardown-contract.md): the compensation entries
        # a PROVIDE-METHOD registered (`emit ... compensate ...` in a method
        # body), the compensation analog of `_deferred_transactional` above. Like
        # a method-registered transactional inverse, a method-body compensation
        # has NO body generator to yield its `_Compensation` disposer into, and
        # adopting it as a sibling `ctx.effect` is UNSOUND: cordis disposes an
        # adopted effect BEFORE the body's `drain`, so the bare disposer would
        # run on a CLEAN unload — firing the offset after a successful commit and
        # destroying the deliverable the emission was (the item 247 bug this
        # closes, left in place for the method-body site). So the entry is parked
        # here and disposed by `drain` once `_committed`/`_aborting` is settled:
        # DISCHARGED on commit, ENQUEUED for Phase 2 on abort (drained after every
        # proof inverse, exactly as the activation-body compensation is). Each
        # entry also joins `_compensations` above so the WAL discharge record and
        # residue introspection cover it uniformly.
        self._deferred_compensations: list = []
        self._pending_compensations: list = []
        # Phase-2 residue (teardown-contract.md's `compensation-residue`):
        # one record per compensation that raised or was skipped past the
        # budget. Best-effort introspection for this activation; the merged
        # G8 audit surface (246) is a separate, later consumer.
        self.compensation_residue: list = []
        # the Phase-2 bound, read ONCE here at activation (teardown-
        # contract.md's config surface: "read once at activation" so a
        # change mid-run cannot move an already-aborting activation's
        # deadline). `_compensation_budget_s` is `None` for "no bound".
        # `_compensation_per_call_ms` is read for config-surface parity
        # across tiers but UNUSED here: python has no in-call preemption of
        # a synchronous host call (teardown-contract.md's per-tier table),
        # so only the between-compensation check (`_compensation_budget_s`)
        # is enforceable on this tier.
        self._compensation_budget_s = _read_bound_seconds(
            _COMPENSATION_BUDGET_ENV, _COMPENSATION_BUDGET_DEFAULT_MS)
        self._compensation_per_call_ms = os.environ.get(_COMPENSATION_PER_CALL_ENV)
        # stateful host resources this activation acquires (`Map.new()`, …), in
        # acquisition order — the instance's migratable state for a hot-swap
        # (see the state-migration section above). Populated by
        # `_register_resource` while `_body` runs, under `install`'s hook.
        self._resources: list = []
        # item 245: the session commit-state owner (docs/design/245-session-commit.md),
        # or None. When set (a driver registered one via `set_session_owner`)
        # this frame joins the owner's live-frame registry — the commit verb's
        # gate target — and its transactional/compensation discharge defers to
        # the session verdict instead of the activation's own clean unload. None
        # is the compatibility clause: drain discharges at unload, byte-identical
        # to the pre-245 world.
        self._owner: "Optional[SessionOwner]" = _SESSION_OWNER
        if self._owner is not None:
            self._owner.register_frame(self)
        # item 245: set by `drain`'s mid-session-withdrawal branch, so the
        # activation-body inverses that unwind after `drain` escrow themselves
        # (see `_hold_for_session`). Never set by a mid-activation failure.
        self._holding = False
        try:
            _FRAME_BY_CTX[ctx] = self  # so a SpawnHandle can find this frame
        except TypeError:  # pragma: no cover — non-weakrefable ctx
            pass
        # An emitted `apply` resolves its config immediately before building
        # the Frame, so this is where the resolution finally learns which
        # component it belongs to (ConfigSchema itself is name-less in
        # emitted output, and emitted output must stay byte-identical).
        self.config = _flush_config_trace(name)

    def install(self, body: Callable) -> Any:
        """Install the component body (a generator function) as one effect.

        The body is wrapped so this frame is the *current activation* while its
        steps run — that is how a `Map.new()` in the body attributes itself to
        this instance's migratable state. The wrapper re-yields every value
        untouched (including `frame.drain` and any probe-tagged disposer), so
        the LIFO/R1 contract and fault-probe instrumentation are unchanged; it
        only brackets each step with a push/pop of the activation stack.

        item 247: this is also the Phase-2 POST-UNWIND HOOK (teardown-
        contract.md's "the phase split ... by having the compensation disposer
        enqueue itself when invoked during an abort unwind and draining the
        queue in a post-unwind hook"). A synchronous mid-body failure runs the
        WHOLE Phase-1 pass — every bracket/transactional inverse AND every
        `_Compensation.__call__` Phase-1 encounter (which only enqueues, never
        invokes) — inside cordis's own `_fail_setup`, before it re-raises out
        of `self.ctx.effect(...)`. So catching here is exactly "after Phase 1
        has fully completed": `_drain_phase2` now runs every enqueued
        compensation, best-effort, before the original failure propagates.
        Known limitation, honestly not hidden: an ASYNC body's mid-activation
        failure unwinds through cordis's asynchronous single-flight dispose
        path instead of this synchronous one, so this hook does not fire for
        it and any compensations it registered stay enqueued, undrained — py
        preemption is "none" (teardown-contract.md's per-tier table); the
        sync activation path this hook covers is the one every existing
        witnessed/compensate test and the exit-test proof drive."""
        probe = _fault_probe
        if probe is not None and probe.component == self.name:
            body = probe.instrument(body, self)
        try:
            return self.ctx.effect(self._tracked(body), f"{self.name}/body")
        except BaseException:
            self._drain_phase2()
            raise

    def _drain_phase2(self) -> None:
        """Phase 2 of the abort algorithm (teardown-contract.md): run every
        compensation `_Compensation.__call__` enqueued during Phase 1, in the
        order they were enqueued — which is already Phase-2 LIFO (newest
        compensation first), because Phase 1 itself visited the stack newest-
        first and every compensation appended itself to
        `_pending_compensations` in that same encounter order.

        Bounded and best-effort: the deadline (`self._compensation_budget_s`,
        read once at activation from `REVL_COMPENSATION_BUDGET_MS`, `None`
        for no bound) is checked BEFORE each compensation — never mid-call,
        python cannot safely preempt a synchronous host call (teardown-
        contract.md's per-tier table) — and a failure never stops the
        remaining ones: continue-and-record, same rule as Phase 1's bracket/
        transactional failures, `compensation-residue` severity. Every skip
        or failure is recorded, never silently dropped."""
        pending, self._pending_compensations = self._pending_compensations, []
        if not pending:
            return
        budget = self._compensation_budget_s
        deadline = None if budget is None else time.monotonic() + budget
        for entry in pending:
            if deadline is not None and time.monotonic() >= deadline:
                self.compensation_residue.append(_residue_record(
                    entry, outcome="not-attempted", attempted_flag=False,
                    attempted=None,
                    error={"type": "deadline-expired",
                           "message": "phase-2 budget exhausted before "
                                      "this compensation started"}))
                continue
            entry._run_phase2()
            if entry.failed:
                self.compensation_residue.append(_residue_record(
                    entry, outcome="failed", attempted_flag=True,
                    attempted={"phase": 2},
                    error={"type": type(entry.error).__name__,
                           "message": str(entry.error)}))

    def _tracked(self, body: Callable) -> Callable:
        """Wrap `body` so `self` is the top activation frame while its code
        runs between yields. Preserves sync-vs-async-generator identity, which
        cordis's effect dispatch switches on."""
        frame = self

        if inspect.isasyncgenfunction(body):
            async def _async_tracked():
                iterator = body().__aiter__()
                while True:
                    _ACTIVATING.append(frame)
                    try:
                        value = await iterator.__anext__()
                    except StopAsyncIteration:
                        break
                    finally:
                        _ACTIVATING.pop()
                    yield value
            return _async_tracked

        def _tracked_gen():
            iterator = iter(body())
            while True:
                _ACTIVATING.append(frame)
                try:
                    value = next(iterator)
                except StopIteration:
                    break
                finally:
                    _ACTIVATING.pop()
                yield value
        return _tracked_gen

    def adopt(self, effect: Any) -> Any:
        """Join an effect created while ACTIVE to this component's accumulator."""
        self._adopted.append(effect)
        return effect

    def acquire(self, label: str, get: Callable[[], Any], undo: Callable[[Any], Any]) -> Any:
        """A ``let-effect`` step inside a provide-method body: run the
        acquisition through the effect protocol, adopt it, return the value."""
        holder: list = []

        def _setup():
            holder.append(get())
            yield lambda: undo(holder[0])

        self.adopt(self.ctx.effect(_setup, label))
        return holder[0]

    def _wal(self) -> Optional[Any]:
        """The active `WriteAheadLog` reachable through this frame's `ctx`, or
        `None` (bridge slice: wires the Slice-2a runtime to the WAL/recover
        foundation that `backends/python/replay.py`/`src/revl/recovery.py`
        already implement).

        Under `revl run --record`/`--wal`, `replay.Recorder._wrap_apply` calls
        the emitted `apply` with a `_RecordingContext` proxy as `ctx`, carrying
        a `_revl_timeline` attribute set through `object.__setattr__` — a
        genuine instance attribute, not one routed through the proxy's
        `__getattr__` — so a plain `getattr` finds it directly, with no
        threading needed through `emit.py`'s `Frame(_revl_ctx, name)` call
        site. A plain run's `ctx` is the real cordis context with no such
        attribute, so this is a no-op there — every WAL write below becomes a
        no-op too, exactly as an un-recorded run behaved before this slice."""
        timeline = getattr(self.ctx, "_revl_timeline", None)
        if timeline is None:
            return None
        return getattr(timeline, "_wal", None)

    def transactional(self, undo: Callable[[Any], Any], witness: Any) -> "_Transactional":
        """Register a witnessed effect's declared inverse as a TRANSACTIONAL
        entry, carrying its `witness` (item 243). Returns the disposer the
        emitted body yields into the accumulator, so it sits in the same LIFO
        disposer stack as every bracket inverse and unwinds in exact reverse
        order alongside them.

        The distinction from `acquire` lives entirely in the returned disposer
        (:class:`_Transactional`): on teardown it replays `undo(witness)` iff
        this activation did NOT commit (an abort — `drain` never ran), and on a
        clean committed unload it DISCHARGES — the inverse is skipped and the
        witness dropped, because the mutation is the deliverable and must
        persist. Registration itself is unconditional here; the emitted call
        site yields this only on the `Ok` branch (Ok-conditional), so a failed
        mutation that touched nothing schedules no rollback.

        Bridge slice: when a WriteAheadLog is attached, this ALSO writes the
        WAL discharge-descriptor for the entry at registration time (docs/
        design/teardown-contract.md, "WAL descriptor") — durably ahead of
        whether this activation ever commits, so a crash before commit still
        lets `revl recover` reconstruct and replay the inverse. `entry.seq`
        carries the assigned seq so `drain` can name it in the discharge
        record on a clean commit; it is `None` when no WAL is active."""
        entry = _Transactional(self, undo, witness)
        self._transactional.append(entry)
        wal = self._wal()
        if wal is not None:
            record = wal.record_discharge_descriptor(
                "transactional",
                receiver=self.name,
                method=_named_call_method(undo),
                args=[witness],
                origin={"phase": "activation", "key": self.name},
                witness=witness,
            )
            entry.seq = record["seq"]
        return entry

    def transactional_method(self, undo: Callable[[Any], Any], witness: Any) -> "_Transactional":
        """Register a PROVIDE-METHOD witnessed effect's declared inverse as a
        transactional entry on THIS component's activation frame (item 318,
        docs/design/243-witnessed-externs.md). This is the per-tool-call H1
        seam: an agent's fs mutation fires from a provide-method (per request),
        and its inverse must outlive the method call — the method returns, but
        the mutation's rollback must survive until the component/session
        commits or aborts. The enclosing component's activation frame is that
        accumulator: it is component-long, and its commit/abort already drives
        `_Transactional` discharge/replay (Slice 2a).

        Unlike `transactional` (yielded by the activation body's generator into
        cordis's LIFO disposer stack), a method body has no generator to yield
        into. Adopting the entry as a sibling `ctx.effect` is unsound — cordis
        disposes an adopted effect BEFORE the body's `drain`, so on a clean
        unload the disposer would see `_committed` still False and wrongly
        revert the deliverable. So the entry is parked in
        `_deferred_transactional` and disposed by `drain` (commit) or the
        aborting `drain` pass (abort), where `_committed` is already settled.
        The entry still joins `_transactional` so the WAL discharge record and
        residue introspection cover it exactly like an activation-body one.

        Registration is unconditional here; the emitted call site calls this
        only on the `Ok` branch (Ok-conditional), so a failed mutation that
        touched nothing schedules no rollback. Bridge slice: the WAL
        discharge-descriptor is written at registration, durably ahead of the
        commit-vs-abort decision, so a crash before it lets `revl recover`
        reconstruct and replay the inverse (item 243 rule 4)."""
        entry = _Transactional(self, undo, witness)
        self._transactional.append(entry)
        self._deferred_transactional.append(entry)
        wal = self._wal()
        if wal is not None:
            record = wal.record_discharge_descriptor(
                "transactional",
                receiver=self.name,
                method=_named_call_method(undo),
                args=[witness],
                origin={"phase": "call", "key": self.name},
                witness=witness,
            )
            entry.seq = record["seq"]
        return entry

    def abort(self) -> None:
        """Mark this activation as ABORTING so its next teardown reverts rather
        than commits (item 318). A component that activated cleanly reaches its
        final `yield _revl_frame.drain`, so any later `fiber.dispose()` runs
        `drain` and would implicitly commit every accumulated transactional
        entry. A session-level reject of the work (item 245's explicit
        commit/abort UX is the eventual driver) calls this first: `drain` then
        leaves `_committed` False, so the activation-body transactional entries
        AND the per-tool-call deferred entries all replay their inverses and the
        mutations revert, residue-free. Idempotent."""
        self._aborting = True

    def compensation(self, fn: Callable[[], Any]) -> "_Compensation":
        """Register an `emit ... compensate ...` step's offsetting call as a
        COMPENSATION entry on the SAME per-activation LIFO disposer stack as
        every bracket and transactional entry (item 247, docs/design/
        teardown-contract.md). Returns the disposer the emitted body yields
        into the accumulator, so it unwinds in exact reverse-registration
        order alongside them.

        Distinct from both `acquire` and `transactional` (:class:`_Compensation`):
        on a clean committed unload it DISCHARGES — `fn` never runs, because the
        emission it offsets was already the deliverable. On an abort it does
        NOT run inline during Phase 1; it runs in PHASE 2, best-effort, only
        after every bracket/transactional inverse in this activation has
        completed (`Frame.install`'s post-unwind hook drives `_drain_phase2`).
        Registration is unconditional — the call site yields this right after
        the emission, unconditionally on whether the emission itself is
        checked-fallible, matching the existing `emit ... compensate ...`
        surface.

        Bridge slice: when a WriteAheadLog is attached, this ALSO writes the
        WAL discharge-descriptor for the entry at registration time — same
        mechanism as `transactional` (teardown-contract.md, "WAL descriptor"),
        `entry="compensation"` — so a crash before this activation's fate is
        decided still lets `revl recover` reconstruct and re-issue it through
        recovery.py's dedicated `apply_compensation` path (never the generic
        `apply_inverse`, whose `_REMOVE` verb match would wrongly pop the
        forward referent — teardown-contract.md's "WAL descriptor" section).
        `entry.seq` carries the assigned seq so `drain` can name it in the
        discharge record on a clean commit; it is `None` when no WAL is
        active."""
        entry = _Compensation(self, fn, method=_named_call_method(fn))
        self._compensations.append(entry)
        wal = self._wal()
        if wal is not None:
            record = wal.record_discharge_descriptor(
                "compensation",
                receiver=self.name,
                method=_named_call_method(fn),
                args=[],
                origin={"phase": "activation", "key": self.name},
                witness=None,
            )
            entry.seq = record["seq"]
        return entry

    def compensation_method(self, fn: Callable[[], Any]) -> "_Compensation":
        """Register a PROVIDE-METHOD `emit ... compensate ...` step's offsetting
        call as a COMPENSATION entry on THIS component's activation frame (the
        item-247 method-body compensate remainder, docs/design/teardown-contract.md).
        This is the compensation analog
        of `transactional_method` (item 318): a per-tool-call emission fires from
        a provide-method, and its offset must outlive the method call — the method
        returns, but the offset is owed only if the component/session ABORTS, and
        must never fire on a clean commit (the emission was the deliverable).

        Unlike `compensation` (yielded by the activation body's generator into
        cordis's LIFO disposer stack), a method body has no generator to yield
        into. Adopting the `_Compensation` as a sibling `ctx.effect` is UNSOUND —
        cordis disposes an adopted effect BEFORE the body's `drain`, so the
        disposer would run on a CLEAN unload and fire the offset after a
        successful commit (the exact placeholder-lowering bug this closes). So
        the entry is parked in `_deferred_compensations` and disposed by `drain`
        (commit → discharge; abort → enqueue for Phase 2), where the commit-vs-
        abort bit is already settled. The entry still joins `_compensations` so
        the WAL discharge record and residue introspection cover it exactly like
        an activation-body one.

        Registration is unconditional here, matching the activation-body
        `compensation` and the existing `emit ... compensate ...` surface: the
        call site yields this right after the emission, unconditionally. Bridge
        slice: the WAL discharge-descriptor is written at registration, durably
        ahead of the fate decision, `entry="compensation"`, `origin.phase="call"`
        (a per-tool-call crossing, as opposed to the activation-body's
        `"activation"`), so `revl recover` reconstructs and re-issues it through
        recovery's `apply_compensation` path (teardown-contract.md's "WAL
        descriptor").

        Recorder seam: under `--record`, the offset must still surface as a
        `compensation` step in the step-back timeline. The pre-fix placeholder
        lowering got that for free — it `yield`ed the bare offset lambda into a
        recorded effect generator, where `Timeline.record_yield` classified it by
        SOURCE ADJACENCY to the just-recorded emission. Routing through the frame
        bypasses that generator, so this records the step explicitly (same
        classifier) and adopts the ONCE-wrapped disposer `record_yield` returns as
        the entry's `fn` — so a step-back run and the live Phase-2 drain share one
        guard and never double-fire the offset. A plain run carries no timeline
        (its `ctx` has no `_revl_timeline`), so this is a no-op there, byte-inert.
        The discharge-descriptor's `method` is read from the ORIGINAL `fn` before
        wrapping, so the WAL names the real offsetting call, not the wrapper."""
        method_name = _named_call_method(fn)
        timeline = getattr(self.ctx, "_revl_timeline", None)
        if timeline is not None:
            _step, fn = timeline.record_yield(fn, f"{self.name}/compensate")
        entry = _Compensation(self, fn, method=method_name)
        self._compensations.append(entry)
        self._deferred_compensations.append(entry)
        wal = self._wal()
        if wal is not None:
            record = wal.record_discharge_descriptor(
                "compensation",
                receiver=self.name,
                method=method_name,
                args=[],
                origin={"phase": "call", "key": self.name},
                witness=None,
            )
            entry.seq = record["seq"]
        return entry

    def enqueue_deferred(self, receiver: str, method: str, args: list,
                         fire: Callable[[], Any]) -> None:
        """Enqueue a class-(b) deferred emission onto the session's deferral
        queue instead of firing it (item 245, docs/design/245-session-commit.md,
        Decision 3). The host body does NOT run here; it runs exactly once at the
        session commit's flush, or never (on abort). `fire` is the zero-arg thunk
        the flush invokes; `receiver`/`method`/`args` are the serializable
        named-call descriptor for the WAL and the commit manifest.

        Requires a session owner: the deferral queue, the escrow and the commit
        verb that flushes or drops it all live on the owner (the py driver). A
        deferred emission reaching a runtime with no owner is the exact failure
        the five ownerless tiers refuse at emit (Decision 2's tier gate); on py
        the driver is always the owner, so this refusal is a guard against a
        misconfigured embedding, never a normal path."""
        if self._owner is None:
            raise RuntimeError(
                "a deferred emission needs a session owner runtime (the deferral "
                "queue and the commit verb), which this composition has none — "
                "load it under a driver that registers one (item 245)")
        self._owner.enqueue(receiver, method, list(args), fire)

    def request_approval(self, capability: str, fields: dict) -> dict:
        """`let a = await approval[C] { fields }` at runtime (item 246). Resolve a
        standing `Approval[C]` for THIS component from the owner ledger and return
        a handle threaded to the crossing by `with`. Fails closed when the policy
        is enforced and no valid approval covers it (silence never approves). When
        the policy is off, or no owner is registered, returns a passthrough handle
        so a `with a` crossing fires normally (byte-identity)."""
        owner = self._owner
        if owner is None or not owner.approval_enforced:
            return {"capability": capability, "requestId": None,
                    "passthrough": True}
        entry = owner.find_approval(capability, self.name)
        if entry is None:
            raise ApprovalCrossingRefused(
                capability,
                f"no granted, unexpired, unconsumed Approval[{capability}] for "
                f"component `{self.name}` in this generation and session — a "
                f"human must grant it via revl_approve (item 246, "
                f"unreachable-without)")
        return {"capability": capability, "requestId": entry.get("requestId"),
                "passthrough": False}

    def approval_crossing(self, handle: Any, capability: str,
                          fire: Callable[[], Any]) -> Any:
        """`emit <call> with a` at runtime (item 246, Decision 3). The frame
        checks the token BEFORE the host body runs and consumes it DURABLY first
        (consume-before-fire): the `approval-consumed` WAL record is flushed, then
        the body fires, then the `approval-emission` record names the same
        `requestId`. A crash between spend and fire leaves consumed-but-unfired —
        an owed action needing a FRESH approval, fail-closed. When the policy is
        off (passthrough handle / no owner), the body fires unchanged."""
        owner = self._owner
        if owner is None or not owner.approval_enforced \
                or (isinstance(handle, dict) and handle.get("passthrough")):
            return fire()
        # re-resolve at the crossing: the token must still be valid HERE (expiry
        # is checked at the crossing, not at mint — invariant 3), by requestId
        # when the handle names one, else by capability+component.
        rid = handle.get("requestId") if isinstance(handle, dict) else None
        entry = None
        if rid is not None:
            entry = next((e for e in owner.approval_ledger
                          if e.get("requestId") == rid and not e.get("consumed")),
                         None)
            if entry is not None and owner.find_approval(
                    capability, self.name) is None:
                entry = None  # named token is stale/expired/wrong-generation
        if entry is None:
            entry = owner.find_approval(capability, self.name)
        if entry is None:
            raise ApprovalCrossingRefused(
                capability,
                f"the Approval[{capability}] threaded here is absent, consumed, "
                f"expired, or bound to another component/generation/session "
                f"(item 246, invariants 1/3/4/5)")
        owner.consume_approval(entry)          # durable spend BEFORE the fire
        result = fire()                         # the host body crosses now
        wal = self._wal()
        if wal is not None:
            wal.record_approval_emission(entry.get("requestId"), capability,
                                         self.name)
        return result

    def drain(self) -> Any:
        """Dispose every adopted effect, newest first (yielded last by the
        emitted body, so the runtime runs it first on unload).

        item 243: reaching `drain` at teardown is the proof that the body ran to
        its final `yield` — i.e. activation completed and this unload is a clean
        one, an implicit commit. Flip `_committed` first, synchronously, before
        disposing anything: `drain` is yielded last so cordis disposes it FIRST
        (LIFO), which means every transactional inverse collected earlier runs
        AFTER this line and observes the commit. An abort never yields `drain`,
        so this never runs and the transactional inverses replay.

        Bridge slice: because `_committed` is now fixed True, every entry
        already registered in `self._transactional` will discharge (not
        replay) regardless of whether its own `_Transactional.__call__` has
        run yet — so the WAL discharge record for all of them can be, and
        must be, written HERE, before this call returns and success is
        reported to the caller (docs/design/teardown-contract.md, "Commit
        path"). That is what closes the crash window: a process that commits
        and then dies before its `activation-complete` marker still leaves a
        durable discharge record on disk, so `revl recover` skips the
        already-committed mutation instead of wrongly rolling it back.

        item 247: every registered `_Compensation` entry discharges the same
        way (its `__call__` reads the same `_committed` flag) and is owed the
        same durable discharge record — a compensation is never owed on a
        clean unload (teardown-contract.md's "Commit path"), so its seq joins
        the transactional seqs in the one discharge record written here."""
        # item 318: `_aborting` is the reject signal for an already-activated
        # component (a session-level abort of per-tool-call work). When set,
        # `drain` runs but does NOT commit: `_committed` stays False, so every
        # transactional entry — the activation body's cordis-yielded ones AND
        # the method-registered deferred ones below — replays its inverse and
        # the mutations revert. A plain unload does not set it, so this is
        # byte-identical to the previous unconditional commit for every
        # existing activation-body-only program.
        # item 245: a MID-SESSION withdrawal under a session owner whose verdict
        # is still pending (a swap/undo). This drain does NOT commit: the frame's
        # undischarged transactional/compensation entries are ESCROWED to await
        # the session verdict, and no discharge record is written. The
        # activation-body cordis disposers escrow themselves via `_hold_for_session`
        # when the fiber unwinds; the method-registered entries are escrowed here.
        # The frame leaves the live registry (it is being torn down), but its
        # entries live on in the escrow until the session commits or aborts. The
        # bracket (adopted) effects still run — releasing a handle is always right.
        if self._owner is not None and self._owner._verdict is None \
                and not self._aborting:
            self._holding = True
            self._owner.withdraw_frame(self)
            for entry in self._deferred_transactional:
                self._owner.escrow(entry)
                entry._escrowed = True
            self._deferred_transactional = []
            # item 247 (method-body compensate remainder): the method-registered compensations escrow the same way;
            # `finalize_abort` runs their Phase-2 offset (after the escrowed
            # transactional inverses), and a session commit discharges them by
            # omission — byte-for-byte the `_deferred_transactional` discipline.
            for entry in self._deferred_compensations:
                self._owner.escrow(entry)
                entry._escrowed = True
            self._deferred_compensations = []
            return self._dispose_adopted()

        if not self._aborting:
            self._committed = True
        wal = self._wal()
        # The discharge record is the COMMIT proof; it must not be written for
        # an aborting teardown, where the inverses are being replayed, not
        # committed (item 318). Under a session owner the consolidated discharge
        # record is written ONCE by the owner over (escrow + every live frame),
        # so each frame SUPPRESSES its own — otherwise the same seqs would be
        # named twice and the one-record-per-session-commit shape (Decision 1)
        # would not hold.
        if self._committed and wal is not None and self._owner is None:
            seqs = [entry.seq for entry in self._transactional if entry.seq is not None]
            seqs += [entry.seq for entry in self._compensations if entry.seq is not None]
            if seqs:
                wal.record_discharge(seqs)
        # Dispose the method-registered transactional entries HERE, now that the
        # commit-vs-abort bit is settled (item 318): on a commit each discharges
        # (mutation persists, witness GC'd); on an abort each replays (reverts).
        # They are not cordis disposers, so this is their sole disposal — no
        # double-free with the fiber's own unwind.
        #
        # item 369: replay in reverse INVOCATION order (LIFO), NOT registration
        # order. `_deferred_transactional` is appended newest-last as each
        # provide-method fires (`transactional_method`), so it must be drained
        # newest-FIRST — exactly like the activation-body path, where cordis
        # unwinds its disposer stack LIFO, and like `_dispose_adopted` below
        # (`reversed(adopted)`). On a COMMIT order is immaterial (every entry
        # no-op discharges), but on an ABORT two inverses whose paths OVERLAP
        # must undo newest-first or the guarantee 243/246 sell is violated:
        # every stdlib/fs.rvl inverse is idempotent-and-total, so a FIFO replay
        # runs the oldest inverse first, finds nothing, silently no-ops, and the
        # newer inverse then undoes into the hole — residue or DESTROYED
        # pre-session data with `noResidue: true` still reported (G7, 243 §2).
        deferred, self._deferred_transactional = self._deferred_transactional, []
        for entry in reversed(deferred):
            entry()
        # item 247 (method-body compensate remainder): dispose the method-registered COMPENSATION entries, now that
        # the commit-vs-abort bit is settled — the compensation analog of the
        # `_deferred_transactional` disposal above, and the method-body analog of
        # the activation-body `emit ... compensate ...` (item 247). On a COMMIT
        # each `__call__` DISCHARGES (the offset never runs — the emission was the
        # deliverable, best-effort cleanup on success would be wrong); on an ABORT
        # each ENQUEUES onto `_pending_compensations` (never fired inline during
        # Phase 1, so a raising offset can never interrupt a proof inverse).
        # Newest-first, so Phase 2 runs the newest compensation first, matching
        # the activation-body path (cordis unwinds its disposer stack LIFO).
        deferred_comp, self._deferred_compensations = self._deferred_compensations, []
        for entry in reversed(deferred_comp):
            entry()
        adopted = self._dispose_adopted()
        # item 247 (method-body compensate remainder): Phase 2 — actually invoke the enqueued compensations, guarded
        # and residue-collected (`_drain_phase2`), only AFTER every proof inverse
        # in this activation has completed: the transactional replay above AND the
        # adopted bracket disposal. This is the two-phase ordering item 247's
        # activation-body path gets from `install`'s post-unwind hook; a method
        # body has no such generator, so `drain` sequences the phases itself.
        # `_drain_phase2` is a no-op when nothing was enqueued (a commit, or a
        # compensation-free activation), so this is byte-inert for those.
        if adopted is not None:
            async def _drain_after(_co=adopted):
                await _co
                self._drain_phase2()
            return _drain_after()
        self._drain_phase2()
        return None

    def _dispose_adopted(self) -> Any:
        adopted, self._adopted = self._adopted, []
        if not adopted:
            return None

        async def run() -> None:
            for effect in reversed(adopted):
                result = effect()
                if inspect.isawaitable(result):
                    await result

        return run()


# ---------------------------------------------------------------------------
# item 245: the session owner, the deferral queue, the commit/abort verbs
# ---------------------------------------------------------------------------


class SessionCommitError(RuntimeError):
    """A session commit could not proceed as asked — most often a stale manifest
    hash (the queue or the live composition changed since enumeration), which is
    refused rather than flushing a superset of what the human approved."""


class ApprovalCrossingRefused(RuntimeError):
    """A typed-approval crossing (`emit … with a`) that no valid `Approval[C]`
    covers (item 246, invariant 1 runtime half). Raised at the crossing BEFORE
    the host body runs, so a hand-built IR or a backend bug cannot cross a
    sensitive boundary silently. Carries the capability and the binding that
    failed for the why-trace."""

    def __init__(self, capability: str, reason: str) -> None:
        super().__init__(
            f"approval crossing refused for capability `{capability}`: {reason}")
        self.capability = capability
        self.reason = reason


def _default_now_ms() -> int:
    import time  # noqa: PLC0415 — stdlib
    return int(time.time() * 1000)


def _approval_scope_covers(scope: Optional[str], token: str) -> bool:
    """Whether an `Approval[scope]` covers a crossing of capability `token`
    (Decision 3, `C within C'`'s scope). Exact match or a glob scope."""
    if scope is None:
        return False
    if scope == token:
        return True
    from fnmatch import fnmatchcase  # noqa: PLC0415 — stdlib, only on a crossing
    return fnmatchcase(token, scope)


def _hash_manifest(target: dict) -> str:
    """The manifest hash binding the gate target (item 245, Decision 4). Over the
    canonical JSON of the target-defining state — the deferral queue descriptors,
    the escrowed/live witnessed seqs, and the live registry's components — so any
    drift (another enqueue, a swap) recomputes to a different hash and the confirm
    is refused. Deterministic: sorted keys, no whitespace variance."""
    payload = json.dumps(target, sort_keys=True, separators=(",", ":"))
    import hashlib  # noqa: PLC0415 — stdlib, only needed on a commit
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _Deferred:
    """One class-(b) descriptor on the deferral queue (item 245, Decision 3).
    Holds a serializable named-call descriptor for the WAL/manifest and a zero-arg
    thunk that fires the real host body at flush. The host body runs once, at
    flush, or never (on abort)."""

    __slots__ = ("receiver", "method", "args", "_fire", "seq", "fired", "error")

    def __init__(self, receiver: str, method: str, args: list,
                 fire: Callable[[], Any]) -> None:
        self.receiver = receiver
        self.method = method
        self.args = args
        self._fire = fire
        self.seq: Optional[int] = None
        self.fired = False
        self.error: Optional[BaseException] = None

    def descriptor(self) -> dict:
        return {"receiver": self.receiver, "method": self.method,
                "args": list(self.args)}

    def group(self) -> str:
        return f"{self.receiver}.{self.method}"

    def fire(self) -> None:
        """Invoke the host body once (flush). Best-effort — a raise is caught by
        the caller (continue-and-record), never here."""
        self.fired = True
        fire, self._fire = self._fire, None
        if fire is not None:
            fire()


class SessionOwner:
    """The session's commit-state owner (item 245, docs/design/245-session-commit.md).

    One per driver lifetime. Owns the three session-scoped structures the commit
    verb derives its GATE TARGET from — never the runtime's per-call current
    frame (`_FRAME_BY_CTX`):

      * the DEFERRAL QUEUE — a FIFO of class-(b) descriptors, WAL-logged at
        enqueue, flushed (fired once, in program order) on commit, dropped on
        abort;
      * the DISCHARGE ESCROW — already-registered transactional/compensation
        entries of a mid-session withdrawal (a swap/undo), awaiting the verdict;
      * the LIVE-FRAME REGISTRY — every activation frame currently live.

    A session with three live components commits all three or none: the verdict
    is session-scoped, the accumulator is still the activation frame (Decision 1).
    The driver drives the two-phase commit (`enumerate` then `approve`) and the
    abort; this class holds the state and the durable WAL bookkeeping.
    """

    def __init__(self, wal_getter: Optional[Callable[[], Any]] = None) -> None:
        self._queue: list = []       # _Deferred, FIFO
        self._escrow: list = []      # _Transactional/_Compensation of withdrawn frames
        self._registry: list = []    # live Frames, registration order
        self._wal_getter = wal_getter
        self._verdict: Optional[str] = None    # None (pending) | "commit" | "abort"
        self.prompts = {"commit": 0, "perCall": 0, "residue": 0}
        # item 246 (docs/design/246-auto-approve.md, Decision 6): boundary calls
        # counted by posture at decision time. `silent` = class (a),
        # auto-approved on a checked inverse; `atCommit` = class (b), auto-approved
        # and enumerated at commit; `prompted` = class (c), a ticket surfaced to a
        # human. Class none is not a boundary call and stays out of all three
        # (and out of the percent-auto-approved denominator). The auto-approve
        # policy (Session) increments these; they are 0 for a session with no
        # policy configured, so the manifest is byte-identical there.
        self.approvals = {"silent": 0, "atCommit": 0, "prompted": 0}
        self.flush_residue: list = []
        # item 247 gap 2: the compensation residue from ESCROWED entries — the
        # entries of a mid-session-withdrawn frame whose Phase-2 offset runs in
        # `finalize_abort`, after the frame has already left `_registry`. A live
        # frame's residue stays on `frame.compensation_residue` and is merged with
        # this by `collect_compensation_residue` at the session boundary; without
        # this, an escrowed offset's failure was run but never recorded (dropped).
        self.compensation_residue: list = []
        # item 246, Decision 3: the typed-approval ledger, owned here beside the
        # deferral queue and the escrow. Each granted `Approval[C]` is one entry
        # bound to its capability, component, reach-closure candidate hash,
        # session, ttl and single-use `consumed` bit. The language-level frame
        # check (`Frame.approval_crossing`) and the operator-layer ticket path
        # (`Session.approve_ticket`) share THIS ledger — one consent mechanism,
        # two entry points. Empty and inert unless the approval policy is enabled.
        self.approval_ledger: list = []
        # whether the language-level frame check enforces: True only when the
        # session enabled the approval policy. Off = a `with a` crossing fires
        # normally (byte-identity — the edge only exists in new programs).
        self.approval_enforced: bool = False
        # the live generation's reach-closure candidate hash per component,
        # pushed by the session at load/swap. The frame check binds a token to it
        # (invariant 4, candidate-invalidates): a swap changes the hash and every
        # standing token whose closure moved fails the crossing.
        self.approval_candidates: dict = {}
        # the session identity a token is bound to (invariant 5, non-replayable):
        # a token minted in one session is refused in a later one, even over the
        # same workspace and WAL. None until the session sets it.
        self.session_id: Optional[str] = None
        # an injectable clock (ms since epoch) so expiry (invariant 3) is testable
        # without sleeping; defaults to the wall clock.
        self.now_ms: Callable[[], int] = _default_now_ms

    def _wal(self) -> Optional[Any]:
        return self._wal_getter() if self._wal_getter is not None else None

    # -- item 246: the typed-approval ledger -------------------------------

    def grant_approval(self, entry: dict) -> None:
        """Record a granted `Approval[C]` (item 246). Called by the session's
        `approve_ticket`/`await approval` grant; the frame check reads it back at
        the crossing. Idempotent on `requestId`."""
        rid = entry.get("requestId")
        if any(e.get("requestId") == rid for e in self.approval_ledger):
            return
        self.approval_ledger.append(dict(entry))

    def find_approval(self, capability: str, component: str,
                      candidate_hash: Optional[str] = None) -> Optional[dict]:
        """The first VALID standing approval covering this crossing (all five
        bindings, invariants 2-5): unconsumed, unexpired, same capability scope,
        same component, same reach-closure candidate hash against the live
        generation, same session. None when nothing covers it — the crossing then
        fails closed (invariant 1, runtime half)."""
        want_hash = (candidate_hash if candidate_hash is not None
                     else self.approval_candidates.get(component))
        now = self.now_ms()
        for entry in self.approval_ledger:
            if entry.get("consumed"):
                continue
            if entry.get("component") != component:
                continue                                   # invariant 5: deputy
            if not _approval_scope_covers(entry.get("capability"), capability):
                continue                                   # wrong capability
            exp = entry.get("expiresAt")
            if exp is not None and now > exp:
                continue                                   # invariant 3: expired
            if want_hash is not None and entry.get("candidateHash") != want_hash:
                continue                                   # invariant 4: swapped
            if self.session_id is not None \
                    and entry.get("session") not in (None, self.session_id):
                continue                                   # invariant 5: session
            return entry
        return None

    def consume_approval(self, entry: dict) -> None:
        """Spend the token durably BEFORE the crossing fires (Decision 3,
        consume-before-fire). The `approval-consumed` WAL record is written and
        flushed here; a crash between it and the fire leaves consumed-but-unfired
        — fail-closed, a fresh approval is demanded on recover."""
        entry["consumed"] = True
        wal = self._wal()
        if wal is not None:
            wal.record_approval_consumed(entry.get("requestId"))

    # -- registry / escrow -------------------------------------------------

    def register_frame(self, frame: "Frame") -> None:
        if frame not in self._registry:
            self._registry.append(frame)

    def withdraw_frame(self, frame: "Frame") -> None:
        try:
            self._registry.remove(frame)
        except ValueError:
            pass

    def escrow(self, entry: Any) -> None:
        if entry not in self._escrow:
            self._escrow.append(entry)

    # -- deferral queue ----------------------------------------------------

    def enqueue(self, receiver: str, method: str, args: list,
                fire: Callable[[], Any]) -> "_Deferred":
        """Append a class-(b) descriptor and WAL-log it at enqueue (Decision 3).
        The intent is durable here; the outcome is a later `flushed` record."""
        descriptor = _Deferred(receiver, method, list(args), fire)
        self._queue.append(descriptor)
        wal = self._wal()
        if wal is not None:
            record = wal.record_deferred_emission(
                receiver=receiver, method=method, args=list(args))
            descriptor.seq = record["seq"]
        return descriptor

    # -- gate target + manifest (Decision 4) -------------------------------

    def _live_entries(self) -> list:
        """Every undischarged transactional/compensation entry the commit will
        discharge — from every live registry frame plus the whole escrow. This is
        the derivation the design insists on: owner-held state, never a current
        frame."""
        entries: list = []
        for frame in self._registry:
            entries += frame._transactional
            entries += frame._compensations
        entries += self._escrow
        return entries

    def _witnessed_seqs(self) -> list:
        """The WAL seqs of every undischarged witnessed/compensation entry — the
        set the consolidated discharge record names on commit. Empty when no WAL
        is attached (seq is None); the count for the manifest uses
        :meth:`_witnessed_count` instead, which is WAL-independent."""
        return sorted({e.seq for e in self._live_entries() if e.seq is not None})

    def _witnessed_count(self) -> int:
        # `_live_entries` mixes `_Transactional` (has `.replayed`) and
        # `_Compensation` (item 247: has no `.replayed` — a compensation offsets,
        # it never replays an inverse). Read `replayed` defensively so a live
        # compensation entry counts toward the commit gate target (it discharges
        # at commit, joining the discharge record) instead of crashing the
        # manifest — the two-step commit of a session holding a compensation.
        return sum(1 for e in self._live_entries()
                   if not e.discharged and not getattr(e, "replayed", False))

    def _target(self) -> dict:
        """The gate target, hash-bound: (queue, live witnessed count, registry
        composition). Any drift — another enqueue, another witnessed call, a swap
        — changes the hash and refuses confirm. Uses a COUNT, not seqs, so the
        binding holds with or without a WAL attached."""
        return {
            "queue": [d.descriptor() for d in self._queue],
            "witnessed": self._witnessed_count(),
            "registry": sorted(f.name for f in self._registry),
        }

    def manifest(self) -> dict:
        """The commit manifest (Decision 4) — the one schema item 246 freezes
        against. `summary` is the prompt's one line; everything else is the
        evidence behind it. The hash binds the gate target."""
        summary: dict = {}
        for d in self._queue:
            summary[d.group()] = summary.get(d.group(), 0) + 1
        target = self._target()
        return {
            "deferred": [dict(d.descriptor(), site=None, group=d.group())
                         for d in self._queue],
            "summary": [{"group": g, "count": n}
                        for g, n in sorted(summary.items())],
            "fired": [],   # class-(c) crossings are logged at fire; 246 fills this
            "witnessed": {"count": target["witnessed"]},
            "residue": {"clean": True, "outstanding": []},
            "prompts": dict(self.prompts),
            # item 246: the posture tally and the headline percent, so the commit
            # manifest carries the auto-approve metrics beside the prompt counts.
            "approvals": dict(self.approvals),
            "percentAutoApproved": self.percent_auto_approved(),
            "hash": _hash_manifest(target),
        }

    def percent_auto_approved(self) -> Optional[float]:
        """`(silent + atCommit) / (silent + atCommit + prompted)` over calls that
        reached at least one crossing (item 246, Decision 6). None when no
        boundary call has been decided, so the ratio never divides by zero."""
        a = self.approvals
        denom = a["silent"] + a["atCommit"] + a["prompted"]
        if denom == 0:
            return None
        return round(100.0 * (a["silent"] + a["atCommit"]) / denom, 2)

    # -- commit (two-step, hash-bound) -------------------------------------

    def approve(self, manifest_hash: str) -> dict:
        """Confirm the commit against the approved hash (Decision 4). Recomputes
        the target hash; any drift since enumeration refuses. On match: writes
        `commit-approved` durably BEFORE the first fire, sets the verdict, and
        FLUSHES the queue FIFO. Returns a flush report. The caller then unloads
        the frames (which discharge) and calls `finalize_commit`."""
        current = _hash_manifest(self._target())
        if manifest_hash != current:
            raise SessionCommitError(
                "stale manifest hash — the deferral queue or the live "
                "composition changed since enumeration, so the confirm is "
                "refused. Re-enumerate: what fires must be exactly what was "
                "approved (item 245, Decision 4)")
        wal = self._wal()
        if wal is not None:
            wal.record_commit_approved(manifest_hash)
        self._verdict = "commit"
        self.prompts["commit"] += 1
        return self._flush()

    def _flush(self) -> dict:
        """Fire the deferral queue FIFO (program order), the causal order the
        intents were formed in. Each completed fire appends `flushed`; a raise is
        continue-and-record (`flush-residue`), so the remaining queue still
        flushes and the commit still completes."""
        wal = self._wal()
        fired, residue = [], []
        queue, self._queue = self._queue, []
        for d in queue:
            try:
                d.fire()
                fired.append(d.descriptor())
                if wal is not None and d.seq is not None:
                    wal.record_flushed(d.seq)
            except BaseException as error:  # noqa: BLE001 — best-effort, recorded
                d.error = error
                info = {"type": type(error).__name__, "message": str(error)}
                residue.append({"seq": d.seq, **info})
                self.flush_residue.append({"seq": d.seq, "error": info})
                self.prompts["residue"] += 1
                if wal is not None and d.seq is not None:
                    wal.record_flush_residue(d.seq, info)
        return {"fired": fired, "flushResidue": residue}

    def finalize_commit(self) -> list:
        """Write the ONE consolidated discharge record over every committed
        transactional/compensation seq (escrow + live frames), after the flush and
        the frame unload, before `activation-complete` (Decision 1/3). Returns the
        discharged seqs."""
        seqs = self._witnessed_seqs()
        wal = self._wal()
        if wal is not None and seqs:
            wal.record_discharge(seqs)
        return seqs

    # -- abort -------------------------------------------------------------

    def begin_abort(self) -> None:
        """Mark every live registry frame aborting BEFORE any teardown starts
        (Decision 5: the `_aborting` bit must be set before a frame's drain, or
        that drain implicitly commits it), then drop the deferral queue. Zero
        cost, zero crossings — nothing fired, so nothing to invert or offset."""
        self._verdict = "abort"
        for frame in self._registry:
            frame.abort()
        # escrowed frames' entries also revert: they were withdrawn but never
        # committed, so the session abort replays them too. Mark their frames.
        for entry in self._escrow:
            entry.frame.abort()
        self._queue = []   # DROP: no host body runs

    def finalize_abort(self) -> dict:
        """Replay the escrowed entries (reverse-seq, after the live cascade), then
        write the `aborted` completion record naming every seq whose inverse
        actually ran (Decision 5). The absence of `commit-approved` is the
        verdict; this record only lets recover tell a completed abort from a
        crashed one."""
        replayed: list = []
        # escrow replays reverse-seq, in its own two phases (transactional
        # inverses, then owed compensations) — the contract's phase rules.
        escrow = sorted(self._escrow,
                        key=lambda e: (e.seq if e.seq is not None else 0),
                        reverse=True)
        transactional = [e for e in escrow if isinstance(e, _Transactional)]
        compensations = [e for e in escrow if isinstance(e, _Compensation)]
        for entry in transactional:
            entry()   # verdict is settled (abort), so this replays
            if entry.replayed and entry.seq is not None:
                replayed.append(entry.seq)
        for entry in compensations:
            entry._run_phase2()
            # item 247 gap 2: an escrowed offset that raised is residue too —
            # record it (previously run-and-dropped), same shape and severity as
            # the live-frame Phase-2 residue, so the session boundary sees it.
            if entry.failed:
                self.compensation_residue.append(_residue_record(
                    entry, outcome="failed", attempted_flag=True,
                    attempted={"phase": 2},
                    error={"type": type(entry.error).__name__,
                           "message": str(entry.error)}))
        self._escrow = []
        # collect the seqs the live frames replayed too (their inverses ran
        # during the driver's unload)
        for frame in self._registry:
            for entry in frame._transactional:
                if entry.replayed and entry.seq is not None:
                    replayed.append(entry.seq)
        replayed = sorted(set(replayed))
        wal = self._wal()
        if wal is not None:
            wal.record_aborted(replayed)
        return {"replayed": replayed}

    # -- compensation residue (item 247 gap 2) -----------------------------

    def collect_compensation_residue(self) -> list:
        """Every `unresolved` compensation-residue fact this session produced —
        the merge of each live registry frame's `compensation_residue` and the
        escrow residue this owner captured in `finalize_abort` (item 247 gap 2,
        design Decision 2). This is the owner-held enumeration the 246 session
        boundary reads: a session that ends with an offset it could not land
        surfaces it here, never a silently growing in-memory list.

        Deduplicated by identity, so a frame that appears in both the registry
        and (transiently) elsewhere is not double-counted."""
        records: list = []
        seen: set = set()
        for frame in self._registry:
            for rec in getattr(frame, "compensation_residue", ()):  # noqa: B009
                key = id(rec)
                if key not in seen:
                    seen.add(key)
                    records.append(rec)
        for rec in self.compensation_residue:
            key = id(rec)
            if key not in seen:
                seen.add(key)
                records.append(rec)
        return records


# ---------------------------------------------------------------------------
# config resolution
# ---------------------------------------------------------------------------


class ConfigError(TypeError):
    pass


_TYPES = {"Str": str, "Int": int, "Bool": bool}

#: component name -> the configuration it last actually ran with, *after*
#: defaults were applied.  A demo can read this to show what a component
#: really got, not what the host passed in.
RESOLVED_CONFIG: dict = {}

# ConfigSchema is constructed name-less in emitted output; the resolution is
# parked here until the component's Frame (built on the very next line of the
# emitted `apply`) names it.
_pending_config: Optional[tuple] = None


def resolved_config(name: Optional[str] = None):
    """The resolved configuration of ``name`` (or every component's, as a
    dict) — the values a component actually ran with, defaults included."""
    if name is None:
        return {key: dict(value) for key, value in RESOLVED_CONFIG.items()}
    value = RESOLVED_CONFIG.get(name)
    return dict(value) if value is not None else None


def _flush_config_trace(name: str) -> Optional[dict]:
    """Attribute the parked resolution to ``name``, record it, return it."""
    global _pending_config
    if _pending_config is None:
        return None
    resolved, defaulted = _pending_config
    _pending_config = None
    RESOLVED_CONFIG[name] = resolved
    body = ", ".join(f"{key}={json.dumps(resolved[key])}" for key in sorted(resolved))
    tail = f" [defaults: {', '.join(defaulted)}]" if defaulted else ""
    _record(f"{name}.config {{{body}}}{tail}")
    return resolved


class ConfigSchema:
    """Config fields as ``(name, type, default)`` triples; ``default=None``
    means required (IR ``"default": null``).

    The emitted plugin dict carries the schema as ``'Config'``, and
    cordis-py's ``resolve_config`` validates/resolves before ``apply`` runs
    (dict plugins read ``Config`` via ``dict.get`` since fork commit
    ``1c5e6f1`` — see REPORT.md F2). ``validate`` speaks cordis-py's Config
    protocol (``{issues, value}`` result); ``resolve`` is kept for
    hand-written host code that validates eagerly and wants a plain dict.
    """

    def __init__(self, fields: list, name: Optional[str] = None) -> None:
        self.fields = [tuple(field) for field in fields]
        # Optional: emitted output constructs schemas positionally and
        # name-less, but a hand-written host may name one and get the
        # `<name>.config` trace event without a Frame.
        self.name = name

    def _resolve(self, config: Any) -> tuple[dict, list, list]:
        value = dict(config or {})
        defaulted: list = []
        issues = []
        for name, type_name, default in self.fields:
            if name not in value:
                if default is None:
                    issues.append(f'missing required config field "{name}"')
                    continue
                value[name] = default
                defaulted.append(name)
            expected = _TYPES.get(type_name)
            if expected is None:
                continue
            ok = isinstance(value[name], expected)
            if expected is int and isinstance(value[name], bool):
                ok = False  # bool is an int subclass; keep Int honest
            if not ok:
                issues.append(f'config field "{name}" expects {type_name}')
        return value, defaulted, issues

    def _park(self, value: dict, defaulted: list) -> None:
        global _pending_config
        _pending_config = (dict(value), defaulted)
        if self.name is not None:
            _flush_config_trace(self.name)

    def resolve(self, config: Any) -> dict:
        value, defaulted, issues = self._resolve(config)
        if issues:
            raise ConfigError("invalid config:\n" + "\n".join(f"  - {issue}" for issue in issues))
        self._park(value, defaulted)
        return value

    def validate(self, config: Any) -> "_ConfigResult":
        """cordis-py ``Config.validate`` protocol: return ``{issues, value}``
        instead of raising (fiber.resolve_config turns a truthy ``issues``
        into a ValidationError and the fiber lands FAILED). A valid
        resolution is parked exactly like ``resolve`` so the component's
        Frame — built next in the emitted ``apply`` — still attributes the
        ``<name>.config`` trace event and R4 ``resolved_config`` state."""
        value, defaulted, issues = self._resolve(config)
        if not issues:
            self._park(value, defaulted)
        return _ConfigResult(issues, value)


class _ConfigResult:
    """The ``{issues, value}`` object cordis-py's ``resolve_config`` expects."""

    __slots__ = ("issues", "value")

    def __init__(self, issues: list, value: dict) -> None:
        self.issues = issues
        self.value = value


# ---------------------------------------------------------------------------
# string interpolation (`format` expressions)
# ---------------------------------------------------------------------------


def fmt(template: str, *args: Any) -> str:
    """Substitute ``$0``, ``$1``… placeholders; ``$$`` is a literal dollar
    (IR v1/A4: split on placeholders first, then unescape)."""
    parts = re.split(r"(\$\$|\$\d+)", template)
    out = []
    for part in parts:
        if part == "$$":
            out.append("$")
        elif part.startswith("$") and part[1:].isdigit():
            out.append(str(args[int(part[1:])]))
        else:
            out.append(part)
    return "".join(out)


# ---------------------------------------------------------------------------
# stub stdlib: host builtins
# ---------------------------------------------------------------------------


class _Closable:
    _serial = 0

    def __init__(self) -> None:
        cls = type(self)
        cls._serial += 1
        self.serial = cls._serial
        self.closed = False
        # attribute this resource to the activation acquiring it, so a hot-swap
        # of the owning instance can capture and migrate its state. Inert
        # outside a component body (a bare `Map.new()` in a test tracks nothing).
        _register_resource(self)

    @property
    def _tag(self) -> str:
        return f"{type(self).__name__.lower()}#{self.serial}"

    def _check_open(self, op: str) -> None:
        if self.closed:
            raise RuntimeError(f"{self._tag}.{op} after close/drop — use-after-free")


# .. _pool-job-semantics:
#
# Pool and Job — the cross-tier semantics
# =======================================
#
# `Pool` and `Job` used to be no-ops ("functional placeholders" in
# docs/v2.0-roadmap.md).  They are now real state machines.  This block is
# the single normative definition; backends/typescript/runtime.ts,
# backends/rust/emit.py::_emit_host_stubs and
# backends/java/emit.py::_emit_pool_runtime/_emit_job_runtime implement
# exactly this and reference it by name.  Divergence between tiers is this
# project's recurring bug class, so the rule is: change this text first,
# then change all four implementations.
#
# Both are dependency-free and deterministic — no driver, no wall-clock, no
# threads, no timers.  Every "wait" is an explicit, bounded number of
# cooperative scheduler turns.
#
# Pool — a bounded connection pool
# --------------------------------
#   Pool.open(url, size)   `size` must be an integer >= 1, else PoolError.
#                          A url starting with "boom://" refuses (the A8
#                          test hook).  Opens with `size` idle connections
#                          numbered 1..size.  Records "<tag>.open <url>".
#   acquire() -> Int       Checks out the lowest-numbered idle connection
#                          (determinism).  PoolError if the pool is closed or
#                          exhausted.  Records
#                          "<tag>.acquire conn=<k> <in_use>/<size>".
#   release(conn)          Returns `conn`.  PoolError if the pool is closed or
#                          `conn` is not currently checked out.  Records
#                          "<tag>.release conn=<k> <in_use>/<size>".
#   query(sql) -> []       Borrows a connection for the duration of the call
#                          (so it fails when the pool is exhausted), records
#                          "<tag>.query <sql>", returns no rows.
#   execute(sql) -> 1      Same borrow, records "<tag>.execute <sql>",
#                          returns 1 (rows affected).
#   close()                PoolError if already closed.  Force-releases every
#                          checked-out connection, then capacity is 0 and
#                          EVERY later operation (including close) raises.
#                          Records "<tag>.close <url>".
#   capacity()/in_use()/available()   accounting, for assertions.
#
#   Invariant: while open, in_use() + available() == capacity() == size;
#   after close(), capacity() == in_use() == available() == 0.
#
#   The borrow inside query/execute is deliberately *silent* (no trace event)
#   so existing trace expectations stay byte-identical; only an explicit
#   acquire/release is traced.
#
# Job — a cancellable asynchronous unit of work
# ---------------------------------------------
#   Job.TICKS = 5          scheduler turns of simulated work.
#   Job.run(name)          -> a handle in state "pending"; records
#                          "job.run <name> start" at *call* time.
#   await handle           Suspends the caller, yielding to the scheduler once
#                          per remaining tick, then state becomes "done",
#                          records "job.run <name> done", and the awaited
#                          value is `name`.  Awaiting a job that is (or
#                          becomes) cancelled raises JobCancelled.  Awaiting a
#                          done job again returns `name` without re-recording.
#   handle.cancel() -> Bool  pending -> "cancelled" (True) and records
#                          "job.run <name> cancelled"; on a done or already
#                          cancelled job it is a no-op returning False.
#                          A divert/teardown that cancels the awaiting task
#                          cancels the job — that is the A1 boundary made
#                          observable.
#   handle.state()         "pending" | "done" | "cancelled"
#   Job.pending()          how many handles are still "pending" — a component
#                          torn down mid-await leaves this > 0 unless the
#                          teardown cancelled.
#   Job.reset()            test helper: forget every handle.
#
# Naming per tier follows that tier's conventions (py `in_use()`, TS
# `inUse()`, rust `in_use()`, java `inUse()`); the state machines, the error
# conditions and the trace strings are identical.  One documented entry-point
# difference: rust splits `Job.run` into `Job::spawn(name) -> JobHandle` (the
# handle, as on the other tiers) and `pub async fn Job::run(name) -> String`
# (the async shorthand the emitted `await Job.run(name)` call site uses), so
# that emitted rust stays a plain `.await`.  Same state machine either way.
# Java has no coroutines, so its handle is driven by `handle.await()` rather
# than by the language's `await`.


class PoolError(RuntimeError):
    """A pool used past its capacity or past its close."""


class Pool(_Closable):
    """A bounded connection pool over a deterministic in-memory database.

    Real pool accounting (see :ref:`pool-job-semantics`): a finite set of
    connections, acquire/release bookkeeping, an exhaustion error, and a
    close that actually releases.  The *database* behind it is still a
    deterministic fake — no driver dependency — so `query` returns no rows
    and `execute` reports one affected row.
    """

    _serial = 0

    def __init__(self, url: str, size: int) -> None:
        super().__init__()
        self.url = url
        self.size = size
        self._idle: list = list(range(1, size + 1))
        self._in_use: list = []
        self.queries: list = []
        self.executed: list = []

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, url: str, size: int) -> "Pool":
        if isinstance(url, str) and url.startswith("boom://"):
            # deliberate test hook: a refusing acquisition, so suites can
            # exercise mid-body failure semantics (IR v1/A8, paper L-Raise)
            _record(f"pool.open refused {url}")
            raise RuntimeError(f"refused to open {url}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise PoolError(f"pool size must be an integer >= 1 (got {size!r})")
        pool = cls(url, size)
        _record(f"{pool._tag}.open {url}")
        return pool

    def close(self) -> None:
        self._check_open("close")
        self._in_use.clear()  # a close releases what is still checked out
        self._idle.clear()
        self.closed = True
        _record(f"{self._tag}.close {self.url}")

    # -- accounting --------------------------------------------------------

    def capacity(self) -> int:
        return 0 if self.closed else self.size

    def in_use(self) -> int:
        return len(self._in_use)

    def available(self) -> int:
        return len(self._idle)

    def _check_open(self, op: str) -> None:
        if self.closed:
            raise PoolError(f"{self._tag}.{op} after close/drop — use-after-free")

    def _borrow(self, op: str) -> int:
        self._check_open(op)
        if not self._idle:
            raise PoolError(
                f"{self._tag}.{op} exhausted (size={self.size}, in_use={len(self._in_use)})"
            )
        conn = self._idle.pop(0)
        self._in_use.append(conn)
        return conn

    def _give_back(self, conn: int) -> None:
        self._in_use.remove(conn)
        self._idle.append(conn)
        self._idle.sort()

    # -- explicit acquire/release (traced) ---------------------------------

    def acquire(self) -> int:
        conn = self._borrow("acquire")
        _record(f"{self._tag}.acquire conn={conn} {len(self._in_use)}/{self.size}")
        return conn

    def release(self, conn: int) -> None:
        self._check_open("release")
        if conn not in self._in_use:
            raise PoolError(f"{self._tag}.release conn={conn} is not checked out")
        self._give_back(conn)
        _record(f"{self._tag}.release conn={conn} {len(self._in_use)}/{self.size}")

    # -- statements (borrow a connection for the call) ---------------------

    def query(self, sql: str) -> list:
        conn = self._borrow("query")
        try:
            self.queries.append(sql)
            _record(f"{self._tag}.query {sql}")
            return []
        finally:
            self._give_back(conn)

    def execute(self, sql: str) -> int:
        conn = self._borrow("execute")
        try:
            self.executed.append(sql)
            _record(f"{self._tag}.execute {sql}")
            return 1
        finally:
            self._give_back(conn)


class JobCancelled(RuntimeError):
    """Raised when a cancelled job is awaited."""


class JobHandle:
    """One asynchronous unit of work (see :ref:`pool-job-semantics`).

    Awaitable, cancellable, and observable while in flight — `await
    Job.run(...)` therefore exercises the A1 divert boundary against real
    async state instead of a no-op.  Determinism: the work is exactly
    ``Job.TICKS`` cooperative scheduler turns, never a timer.
    """

    __slots__ = ("name", "serial", "_state", "_remaining")

    def __init__(self, name: str) -> None:
        Job._serial += 1
        self.serial = Job._serial
        self.name = name
        self._state = "pending"
        self._remaining = Job.TICKS
        Job._handles.append(self)
        _record(f"job.run {name} start")

    def state(self) -> str:
        return self._state

    @property
    def remaining(self) -> int:
        return self._remaining

    def cancel(self) -> bool:
        """pending -> cancelled (True); a no-op returning False otherwise."""
        if self._state != "pending":
            return False
        self._state = "cancelled"
        _record(f"job.run {self.name} cancelled")
        return True

    async def _drive(self) -> str:
        import asyncio  # noqa: PLC0415 — only needed when a job is awaited

        if self._state == "done":
            return self.name
        if self._state == "cancelled":
            raise JobCancelled(f'job "{self.name}" cancelled')
        try:
            while self._remaining > 0:
                await asyncio.sleep(0)
                if self._state == "cancelled":
                    raise JobCancelled(f'job "{self.name}" cancelled')
                self._remaining -= 1
        except asyncio.CancelledError:
            # a divert (or any teardown) during the await cancels the job
            self.cancel()
            raise
        self._state = "done"
        Job.runs.append(self.name)
        _record(f"job.run {self.name} done")
        return self.name

    def __await__(self):
        return self._drive().__await__()

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<job#{self.serial} {self.name} {self._state}>"


class Job:
    """Async host builtin (IR v1): `Job.run(name)` schedules a cancellable
    unit of work and returns an awaitable handle — see
    :ref:`pool-job-semantics`."""

    TICKS = 5

    runs: list = []          # names of jobs that completed (back-compatible)
    _handles: list = []
    _serial = 0

    @classmethod
    def run(cls, name: str) -> JobHandle:
        return JobHandle(name)

    @classmethod
    def pending(cls) -> int:
        """Handles still in flight — teardown residue, made countable."""
        return sum(1 for handle in cls._handles if handle.state() == "pending")

    @classmethod
    def handles(cls) -> list:
        return list(cls._handles)

    @classmethod
    def reset(cls) -> None:
        cls._handles.clear()
        cls.runs.clear()
        cls._serial = 0


# ---------------------------------------------------------------------------
# Time as a coeffect (roadmap item 57, docs/time-coeffect.md)
# ---------------------------------------------------------------------------
#
# A timer (`every 30s { … }` / `after 5m { … }`) is a *revertible schedule*:
# arming it registers a firing with the clock, and its inverse is cancellation,
# so the emitted body yields `lambda: handle.cancel()` and the component's own
# teardown drains it like any other effect (no orphaned interval — the R1/R4
# residue proof catches a leak through the same `schedule`/`cancel` acquire
# pair Pool.open/close uses).
#
# Determinism is the whole point: the clock is a *coeffect the harness
# provides*, not wall-clock. `Clock.now()` only moves when something calls
# `Clock.advance(ms)` — `revl test`/replay drives it — so a firing is a
# deterministic step in the timeline (`fires on the 3rd tick`), never a race,
# and the fault sweep (item 30) can inject "fail at the third firing". A
# production driver would pump `advance` from a real monotonic source; the
# reference tier keeps it explicit so tests are reproducible.


class TimerHandle:
    """One armed timer. Live until it is cancelled (`every`, and an `after`
    before it fires) or has fired to completion (`after`). Cancellation is the
    schedule's inverse — idempotent, and a no-op once the timer is spent."""

    __slots__ = ("serial", "mode", "interval_ms", "_body", "state", "next_at", "fired")

    def __init__(self, mode: str, interval_ms: int, body: Callable[[], Any]) -> None:
        if mode not in ("every", "after"):  # pragma: no cover — emitter invariant
            raise ValueError(f"timer mode must be 'every' or 'after', got {mode!r}")
        if not isinstance(interval_ms, int) or interval_ms <= 0:  # pragma: no cover
            raise ValueError(f"timer interval must be a positive int (ms), got {interval_ms!r}")
        Clock._serial += 1
        self.serial = Clock._serial
        self.mode = mode
        self.interval_ms = interval_ms
        self._body = body
        self.state = "live"
        self.next_at = Clock._now_ms + interval_ms
        self.fired = 0
        Clock._timers.append(self)
        _record(f"{self._tag}.schedule {mode} {interval_ms}ms")

    @property
    def _tag(self) -> str:
        return f"timer#{self.serial}"

    def cancel(self) -> bool:
        """live -> cancelled (True); a no-op returning False once spent. The
        derived inverse the emitted body yields — running it on teardown proves
        the schedule leaves no residue."""
        if self.state != "live":
            return False
        self.state = "cancelled"
        _record(f"{self._tag}.cancel")
        return True

    def _fire(self) -> None:
        self.fired += 1
        Clock._firings.append((self.serial, Clock._now_ms))
        _record(f"{self._tag}.fire #{self.fired} at {Clock._now_ms}ms")
        self._body()
        if self.mode == "after":
            # a one-shot's schedule is spent once it fires; release it through
            # the same `cancel` verb so the residue trace stays balanced and the
            # teardown's own `handle.cancel()` is a clean no-op.
            self.state = "done"
            _record(f"{self._tag}.cancel")

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<timer#{self.serial} {self.mode} {self.interval_ms}ms {self.state}>"


class Clock:
    """The clock coeffect (item 57). Time advances only when the harness calls
    :meth:`advance`; timer firings are deterministic timeline steps, not
    wall-clock races (docs/time-coeffect.md)."""

    _now_ms = 0
    _serial = 0
    _timers: list = []      # every TimerHandle ever armed, registration order
    _firings: list = []     # (timer serial, now_ms) per firing, in fire order

    @classmethod
    def now(cls) -> int:
        return cls._now_ms

    @classmethod
    def advance(cls, ms: int) -> int:
        """Advance logical time by ``ms``, firing every timer that comes due —
        earliest first, ties broken by arm order — and re-arming `every`
        timers across the whole span. Returns the number of firings, so a test
        can assert exactly how many steps an advance produced."""
        if not isinstance(ms, int) or ms < 0:
            raise ValueError(f"clock advance must be a non-negative int (ms), got {ms!r}")
        target = cls._now_ms + ms
        count = 0
        # An event loop rather than a per-timer pass: an `every` re-arms and may
        # fire again within the same advance, and firings must interleave across
        # timers in true time order. Bounded because each iteration consumes one
        # firing and every `next_at` strictly increases (interval_ms > 0).
        while True:
            due = [t for t in cls._timers
                   if t.state == "live" and t.next_at <= target]
            if not due:
                break
            timer = min(due, key=lambda t: (t.next_at, t.serial))
            cls._now_ms = timer.next_at
            if timer.mode == "every":
                timer.next_at += timer.interval_ms
            timer._fire()
            count += 1
        cls._now_ms = target
        return count

    @classmethod
    def pending(cls) -> int:
        """Live timers — a teardown that abandons one leaves this > 0 (mirrors
        ``Job.pending``); the countable form of the no-orphaned-interval proof."""
        return sum(1 for t in cls._timers if t.state == "live")

    @classmethod
    def firings(cls) -> list:
        """The recorded firing log: ``(timer serial, fired-at ms)`` in order."""
        return list(cls._firings)

    @classmethod
    def reset(cls) -> None:
        cls._now_ms = 0
        cls._serial = 0
        cls._timers = []
        cls._firings = []


def schedule_every(interval_ms: int, body: Callable[[], Any]) -> TimerHandle:
    """Arm a periodic timer against the clock coeffect (`every`). The returned
    handle's :meth:`~TimerHandle.cancel` is the schedule's inverse."""
    return TimerHandle("every", interval_ms, body)


def schedule_after(interval_ms: int, body: Callable[[], Any]) -> TimerHandle:
    """Arm a one-shot delayed timer against the clock coeffect (`after`)."""
    return TimerHandle("after", interval_ms, body)


class Map(_Closable):
    """In-memory key/value store with an explicit drop."""

    _serial = 0

    def __init__(self) -> None:
        super().__init__()
        self.data: dict = {}

    @classmethod
    def new(cls) -> "Map":
        instance = cls()
        _record(f"{instance._tag}.new")
        return instance

    def drop(self) -> None:
        self._check_open("drop")
        self.closed = True
        self.data.clear()
        _record(f"{self._tag}.drop")

    def get(self, key: Any) -> Any:
        self._check_open("get")
        return self.data.get(key)

    # -- iteration surface (docs/stdlib-2.0.md §Map) -----------------------
    # `size()`/`keys()` are the value-Map iteration builtins, and the checker
    # promises them on a host `Map.new()` receiver too (the emitter lowers
    # both as plain method calls on this object). They mirror the value-Map
    # semantics exactly: `keys()` yields the keys in ascending canonical Str
    # order (python str comparison IS code-point order, so sorted() is exact),
    # `size()` is the entry count. Read-only, like `get` — no trace record.

    def keys(self) -> list:
        self._check_open("keys")
        return sorted(self.data)

    def size(self) -> int:
        self._check_open("size")
        return len(self.data)

    def insert(self, key: Any, value: Any) -> None:
        self._check_open("insert")
        self.data[key] = value
        _record(f"{self._tag}.insert {key}")

    def insert_if_absent(self, key: Any, value: Any) -> bool:
        # item 397: the atomic compare-and-set. On this tier the whole method
        # is one synchronous, suspension-free step (no await), so it is atomic
        # by construction under the reference runtime's run-to-completion model
        # (backends/python/runtime.py `.. _pool-job-semantics:`): no task can
        # interleave between the membership test and the insert. Returns whether
        # it inserted; a `false` (key already present) leaves the existing value
        # untouched. The trace records the outcome so the residue prover can
        # fold it into the outstanding-key fingerprint (a `true` counts the key
        # as set, a `false` counts nothing).
        self._check_open("insert_if_absent")
        if key in self.data:
            _record(f"{self._tag}.insert_if_absent {key} -> false")
            return False
        self.data[key] = value
        _record(f"{self._tag}.insert_if_absent {key} -> true")
        return True

    def remove(self, key: Any) -> None:
        self._check_open("remove")
        self.data.pop(key, None)
        _record(f"{self._tag}.remove {key}")

    # -- migratable state (hot-swap with live instances) -------------------
    # A Map *is* an instance's state: its entries. `__revl_state__` snapshots
    # them and `__revl_restore__` writes them into a fresh Map under the
    # successor template, so a hot-swap of the owning instance preserves the
    # store rather than starting cold. A resource without this pair (e.g. a
    # `Pool`, whose checkouts are transient) migrates as a fresh equivalent.

    def __revl_state__(self) -> dict:
        self._check_open("__revl_state__")
        return dict(self.data)

    def __revl_restore__(self, state: dict) -> None:
        self._check_open("__revl_restore__")
        self.data = dict(state)

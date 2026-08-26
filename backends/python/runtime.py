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
import re
import weakref
from typing import Any, Callable, Optional

__all__ = [
    "Clock", "ConfigError", "ConfigSchema", "FaultProbe", "Frame", "Job", "JobCancelled",
    "JobHandle", "Map", "Pool", "PoolError", "SpawnHandle", "StateIncompatible",
    "TimerHandle", "TransientError", "add_trace", "arm_fault_probe", "disarm_fault_probe",
    "fmt", "live_instances", "plug", "realm_label", "remove_trace", "resolved_config",
    "retry_idempotent", "schedule_after", "schedule_every", "set_trace", "spawn",
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

    __slots__ = ("frame", "witness", "_undo", "discharged", "replayed")

    def __init__(self, frame: "Frame", undo: Callable[[Any], Any], witness: Any) -> None:
        self.frame = frame
        self._undo = undo
        self.witness = witness
        self.discharged = False   # committed: inverse skipped, mutation persists
        self.replayed = False     # aborted: inverse ran, mutation reverted

    def __call__(self) -> Any:
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
        # stateful host resources this activation acquires (`Map.new()`, …), in
        # acquisition order — the instance's migratable state for a hot-swap
        # (see the state-migration section above). Populated by
        # `_register_resource` while `_body` runs, under `install`'s hook.
        self._resources: list = []
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
        only brackets each step with a push/pop of the activation stack."""
        probe = _fault_probe
        if probe is not None and probe.component == self.name:
            body = probe.instrument(body, self)
        return self.ctx.effect(self._tracked(body), f"{self.name}/body")

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
        mutation that touched nothing schedules no rollback."""
        entry = _Transactional(self, undo, witness)
        self._transactional.append(entry)
        return entry

    def drain(self) -> Any:
        """Dispose every adopted effect, newest first (yielded last by the
        emitted body, so the runtime runs it first on unload).

        item 243: reaching `drain` at teardown is the proof that the body ran to
        its final `yield` — i.e. activation completed and this unload is a clean
        one, an implicit commit. Flip `_committed` first, synchronously, before
        disposing anything: `drain` is yielded last so cordis disposes it FIRST
        (LIFO), which means every transactional inverse collected earlier runs
        AFTER this line and observes the commit. An abort never yields `drain`,
        so this never runs and the transactional inverses replay."""
        self._committed = True
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

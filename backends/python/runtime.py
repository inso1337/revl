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
from typing import Any, Callable, Optional

__all__ = [
    "ConfigError", "ConfigSchema", "FaultProbe", "Frame", "Job", "JobCancelled",
    "JobHandle", "Map", "Pool", "PoolError", "SpawnHandle", "add_trace",
    "arm_fault_probe", "disarm_fault_probe", "fmt", "plug", "realm_label",
    "remove_trace", "resolved_config", "set_trace", "spawn", "trace_observers",
]


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

    __slots__ = ("_fiber", "component", "_disposed")

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
        return self._fiber.dispose()

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
    return SpawnHandle(fiber, component.get("name"))


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
        # An emitted `apply` resolves its config immediately before building
        # the Frame, so this is where the resolution finally learns which
        # component it belongs to (ConfigSchema itself is name-less in
        # emitted output, and emitted output must stay byte-identical).
        self.config = _flush_config_trace(name)

    def install(self, body: Callable) -> Any:
        """Install the component body (a generator function) as one effect."""
        probe = _fault_probe
        if probe is not None and probe.component == self.name:
            body = probe.instrument(body, self)
        return self.ctx.effect(body, f"{self.name}/body")

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

    def drain(self) -> Any:
        """Dispose every adopted effect, newest first (yielded last by the
        emitted body, so the runtime runs it first on unload)."""
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

    Resolved inside the emitted ``apply`` rather than through the runtime's
    ``resolve_config``: cordis-py reads ``Config`` off dict plugins with
    ``getattr``, which never sees dict keys — see REPORT.md.
    """

    def __init__(self, fields: list, name: Optional[str] = None) -> None:
        self.fields = [tuple(field) for field in fields]
        # Optional: emitted output constructs schemas positionally and
        # name-less, but a hand-written host may name one and get the
        # `<name>.config` trace event without a Frame.
        self.name = name

    def resolve(self, config: Any) -> dict:
        global _pending_config
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
        if issues:
            raise ConfigError("invalid config:\n" + "\n".join(f"  - {issue}" for issue in issues))
        _pending_config = (dict(value), defaulted)
        if self.name is not None:
            _flush_config_trace(self.name)
        return value


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

    def insert(self, key: Any, value: Any) -> None:
        self._check_open("insert")
        self.data[key] = value
        _record(f"{self._tag}.insert {key}")

    def remove(self, key: Any) -> None:
        self._check_open("remove")
        self.data.pop(key, None)
        _record(f"{self._tag}.remove {key}")

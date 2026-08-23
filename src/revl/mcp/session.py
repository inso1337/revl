"""A live composition an agent can drive in memory.

`revl run` boots a composition and holds it for a human at a REPL. This is
the same thing for a machine: no files, no stdout trace (which would corrupt
the JSON-RPC stream), and every step reported as data.

The point is the loop it enables. An agent can *generate → check → admit →
load → call → assert-no-residue → fix* without a candidate component ever
existing on disk: a rejected or misbehaving draft leaves nothing behind to
clean up, and only code that has already proven itself gets written out.

Implementation note: `run._Driver` already knows how to emit, load and tear
down a composition, so this subclasses it and redirects the trace instead of
duplicating the lifecycle. The event loop is owned here and advanced per
call — between tool calls the composition is simply idle.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from ..holes import collect as collect_holes
from ..holes import summarize as summarize_holes


class SessionError(RuntimeError):
    """The session cannot do what was asked (no runtime, nothing loaded…)."""


def _backend():
    """Import the cordis-py runtime, with the same guidance `revl run` gives."""
    backend_dir = Path(__file__).resolve().parents[3] / "backends" / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    try:
        import emit  # noqa: PLC0415 — backend import after path setup
        import runtime as runtime_mod  # noqa: PLC0415
        from cordis import Context  # noqa: PLC0415
        from cordis.fiber import FiberState  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        raise SessionError(
            f"the cordis-py runtime is not installed ({exc.name!r} missing) — "
            f"set it up with `sh {backend_dir / 'setup.sh'}` and run the server "
            f"under {backend_dir / '.venv' / 'bin' / 'python'}"
        ) from exc
    return emit, runtime_mod, Context, FiberState


def replay_module():
    """The backwards-replay engine (`backends/python/replay.py`).

    Imported on its own path because it needs no cordis: the timeline, the
    step-back and the forward plan are pure python over the accumulator, so
    they can be exercised without a runtime installed.
    """
    backend_dir = Path(__file__).resolve().parents[3] / "backends" / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import replay  # noqa: PLC0415 — backend import after path setup

    return replay


def _capturing_driver_class():
    """`run._Driver`, with its trace captured instead of printed."""
    from ..run import _Driver  # noqa: PLC0415 — lazy: importing run pulls cordis

    class _CapturingDriver(_Driver):
        def __init__(self, *args, **kwargs):
            self.events: list[dict] = []
            super().__init__(*args, **kwargs)

        def _log(self, channel: str, subject: str, detail: str = "") -> None:
            self.events.append({"channel": channel, "subject": subject,
                                "detail": detail})

        def drain_events(self) -> list[dict]:
            events, self.events = self.events, []
            return events

    return _CapturingDriver


class Session:
    """One live composition, driven step by step."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._driver = None
        self.ir: dict | None = None
        self.previous: dict | None = None  # the generation `rollback` restores
        self.config: dict = {}
        self.recorder = None  # replay.Recorder, when loaded with record=True
        # the admission inputs that produced the live composition — the
        # *sources*, kept so the composition can be snapshotted for
        # re-admission (docs/persistence.md). None when a composition was
        # loaded without them (e.g. a hand-built IR); such a session cannot be
        # snapshotted, because there is nothing to replay through the gate.
        self.origin: dict | None = None
        self.previous_origin: dict | None = None  # origin `rollback` restores

    # -- plumbing ----------------------------------------------------------

    def _run(self, coro):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    @property
    def loaded(self) -> bool:
        return self._driver is not None

    def _require(self):
        if self._driver is None:
            raise SessionError("nothing is loaded — call revl_load first")
        return self._driver

    # -- lifecycle ---------------------------------------------------------

    def load(self, ir: dict, config: dict | None = None,
             record: bool = False, origin: dict | None = None) -> dict:
        if self._driver is not None:
            raise SessionError("a composition is already loaded — swap or unload it")
        # booting is admission: a draft with open obligations is checkable but
        # not runnable, and the refusal belongs here rather than in the Python
        # emitter's lap (docs/holes.md)
        open_holes = collect_holes(ir)
        if open_holes:
            raise SessionError(
                f"cannot load: {len(open_holes)} open typed hole(s) — "
                f"{summarize_holes(open_holes)}. A hole has a type and no "
                f"implementation, so it may never enter a running composition; "
                f"`revl_check` lists them (docs/holes.md)")
        emit, runtime_mod, Context, FiberState = _backend()
        driver_class = _capturing_driver_class()
        self.config = config or {}
        self._driver = driver_class(ir, self.config, emit, runtime_mod, Context, FiberState)
        self.ir = ir
        self.origin = origin
        self.recorder = replay_module().Recorder(ir) if record else None
        self._run(self._driver._load(ir, self._prepare_module(ir)))
        return self.state(drain=True) | ({"recording": True} if record else {})

    def _prepare_module(self, ir: dict):
        """Emit the module and, when recording, instrument it before load.

        Instrumentation has to happen between emit and `plugin`, because it
        replaces each component's `apply` — the fiber's context chain is fixed
        at plugin time and there is no way in afterwards.
        """
        driver = self._driver
        module = driver._emit_module(ir)
        if self.recorder is not None:
            filename, source = driver.emitted
            self.recorder.register_source(filename, source)
            self.recorder.activation_origin()
            self.recorder.timelines.clear()
            self.recorder.instrument(module, ir)
        return module

    def swap(self, ir: dict, origin: dict | None = None) -> dict:
        """Replace the running composition. The caller has already had the
        candidate admitted; this performs the transition."""
        driver = self._require()
        self.previous = self.ir
        self.previous_origin = self.origin
        self._run(driver._dispose_all(self.ir))
        driver.ir = self.ir = ir
        self.origin = origin
        self._run(driver._load(ir, self._prepare_module(ir)))
        return self.state(drain=True)

    def rollback(self) -> dict:
        if self.previous is None:
            raise SessionError("no previous generation to roll back to")
        restored, self.previous = self.previous, None
        restored_origin, self.previous_origin = self.previous_origin, None
        return self.swap(restored, origin=restored_origin) | {"rolledBack": True}

    # -- persistence (docs/persistence.md) ---------------------------------

    def snapshot(self) -> dict:
        """`{sources, manifest, meta}` for the live composition — the inputs
        needed to *re-admit* it, not a dump of runtime objects."""
        from .persist import snapshot as _snapshot  # noqa: PLC0415 — lazy

        return _snapshot(self)

    def restore(self, snap: dict) -> dict:
        """Re-admit a snapshot into this (empty) session by replaying
        admission — compile through the gate, then boot. A component the
        current checker rejects fails loudly and loads nothing."""
        from .persist import restore as _restore  # noqa: PLC0415 — lazy

        return _restore(self, snap)

    def unload(self) -> dict:
        """Tear down and report the residue checks — R4 from inside the
        protocol, so an agent can prove its component leaves nothing."""
        driver = self._require()
        self._run(driver._dispose_all(self.ir))
        checks = {
            "registry": driver.root.registry.size == 0,
            "provisions": driver.root.reflect.store == {},
            "effects": (driver.root.fiber._disposables.length
                        == driver._baseline_disposables),
            "listeners": driver._hooks() == driver._baseline_hooks,
        }
        detail = {
            "registrySize": driver.root.registry.size,
            "provisions": sorted(driver.root.reflect.store),
            "disposables": driver.root.fiber._disposables.length,
            "disposablesBaseline": driver._baseline_disposables,
        }
        driver.runtime.set_trace(None)
        events = driver.drain_events()
        self._driver = None
        self.ir = None
        self.previous = None
        self.origin = None
        self.previous_origin = None
        return {
            "unloaded": True,
            "noResidue": all(checks.values()),
            "checks": checks,
            "detail": detail,
            "trace": events,
        }

    # -- interaction -------------------------------------------------------

    def call(self, key: str, method: str, args: list | None = None) -> dict:
        """Invoke a provided service operation on the running composition —
        how an agent actually *tests* what it just loaded."""
        driver = self._require()
        namespace = driver._namespace()
        if key not in namespace:
            raise SessionError(f"no provided key {key!r} "
                               f"(provided: {', '.join(sorted(namespace)) or 'none'})")
        service = namespace[key]
        if service is None:
            raise SessionError(f"key {key!r} is declared but not currently provided "
                               "— its provider is inactive")
        target = getattr(service, method, None)
        if target is None or not callable(target):
            raise SessionError(f"`{key}.{method}` is not callable on the provided value")

        async def invoke():
            result = target(*(args or []))
            if hasattr(result, "__await__"):
                result = await result
            await driver._flush()
            return result

        # tag whatever this call accumulates with the invocation that caused
        # it — that provenance is what makes forward replay expressible
        if self.recorder is not None:
            self.recorder.set_origin({"phase": "call", "key": key,
                                      "method": method, "args": list(args or [])})
        try:
            result = self._run(invoke())
        finally:
            if self.recorder is not None:
                self.recorder.activation_origin()
        return {"result": _plain(result), "trace": driver.drain_events()}

    # -- backwards replay (docs/replay.md) ---------------------------------

    def _timeline(self, component: str | None):
        if self.recorder is None:
            raise SessionError(
                "this composition was not loaded with recording on — call "
                "revl_load with `record: true` (recording has to be installed "
                "before activation, so it cannot be turned on retroactively)")
        replay = replay_module()
        try:
            return self.recorder.timeline(component)
        except replay.ReplayError as error:
            raise SessionError(str(error)) from None

    def timeline(self, component: str | None = None) -> dict:
        """The recorded accumulator: every effect step in order, its inverse,
        and every emission — marked as the one thing that has none."""
        if self.recorder is None:
            raise SessionError(
                "this composition was not loaded with recording on — call "
                "revl_load with `record: true`")
        if component is None:
            return self.recorder.as_dict()
        return self._timeline(component).as_dict()

    def inspect_step(self, component: str | None, at: int) -> dict:
        return self._timeline(component).inspect(at)

    def bisect(self, component: str | None, predicate: str) -> dict:
        """Binary-search the recorded timeline for the first step at which
        `predicate` flips — git-bisect for an execution. Read-only: the
        predicate is evaluated over the `inspect` reconstruction of each probed
        step, so the timeline is never mutated and the answer is reached in
        log2(N) evaluations. The report carries the found step's full record and
        the verified/unverified status of the effects on the path to it."""
        replay = replay_module()
        timeline = self._timeline(component)
        try:
            return timeline.bisect(predicate)
        except replay.ReplayError as error:
            raise SessionError(str(error)) from None

    def step_back(self, component: str | None, to: int,
                  force: bool = False) -> dict:
        """Unwind the accumulator to step `to`, leaving the component LIVE.

        This is not a teardown: no fiber is disposed, the provisions that
        survive stay callable, and the composition can be inspected and
        stepped forward again.
        """
        replay = replay_module()
        timeline = self._timeline(component)
        try:
            report = self._run(timeline.step_back(to, force=force))
        except replay.IrreversibleStep as error:
            raise SessionError(str(error)) from None
        except replay.ReplayError as error:
            raise SessionError(str(error)) from None
        if self._driver is not None:
            self._run(self._driver._flush())
            report["trace"] = self._driver.drain_events()
            report["providedKeys"] = sorted(
                k for k, v in self._driver._namespace().items() if v is not None)
        return report

    def replay_forward(self, component: str | None, frm: int) -> dict:
        """Re-run the tail after step `frm` by re-invoking the calls that
        produced it. Activation-body steps are reported as not replayable
        rather than faked — see docs/replay.md."""
        timeline = self._timeline(component)
        plan = timeline.forward_plan(frm)
        replayed = []
        for item in plan["replay"]:
            if item.get("kind") != "call":
                # a REPL-origin step (`revl run --record`); the session has no
                # REPL to re-evaluate it in, so it is reported, not guessed at
                replayed.append({**item, "error": "not replayable from a session "
                                                  "— this step came from a REPL line"})
                continue
            outcome = {"key": item["key"], "method": item["method"],
                       "args": item["args"]}
            try:
                outcome["result"] = self.call(item["key"], item["method"],
                                              item["args"])["result"]
            except Exception as exc:  # noqa: BLE001 — a failed replay is a result
                outcome["error"] = f"{type(exc).__name__}: {exc}"
            replayed.append(outcome)
        return {**plan, "replayed": replayed}

    def state(self, drain: bool = False) -> dict:
        if self._driver is None:
            return {"loaded": False}
        driver = self._driver
        manifest = (self.ir or {}).get("manifest") or {}
        return {
            "loaded": True,
            "components": [
                {"name": name,
                 "state": driver.FiberState(fiber.state).name}
                for name, fiber in driver.fibers.items()
            ],
            "loadOrder": manifest.get("loadOrder") or [],
            "providedKeys": sorted(driver._namespace()),
            "canRollback": self.previous is not None,
            "recording": self.recorder is not None,
            **({"trace": driver.drain_events()} if drain else {}),
        }


def _plain(value):
    """Best-effort JSON-able rendering of a service call's result."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if hasattr(value, "__dict__") and vars(value):
        return {k: _plain(v) for k, v in vars(value).items() if not k.startswith("_")}
    return repr(value)

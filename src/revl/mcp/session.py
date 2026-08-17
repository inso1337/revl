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

    def load(self, ir: dict, config: dict | None = None) -> dict:
        if self._driver is not None:
            raise SessionError("a composition is already loaded — swap or unload it")
        emit, runtime_mod, Context, FiberState = _backend()
        driver_class = _capturing_driver_class()
        self.config = config or {}
        self._driver = driver_class(ir, self.config, emit, runtime_mod, Context, FiberState)
        self.ir = ir
        self._run(self._driver._load(ir, self._driver._emit_module(ir)))
        return self.state(drain=True)

    def swap(self, ir: dict) -> dict:
        """Replace the running composition. The caller has already had the
        candidate admitted; this performs the transition."""
        driver = self._require()
        self.previous = self.ir
        self._run(driver._dispose_all(self.ir))
        driver.ir = self.ir = ir
        self._run(driver._load(ir, driver._emit_module(ir)))
        return self.state(drain=True)

    def rollback(self) -> dict:
        if self.previous is None:
            raise SessionError("no previous generation to roll back to")
        restored, self.previous = self.previous, None
        return self.swap(restored) | {"rolledBack": True}

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

        result = self._run(invoke())
        return {"result": _plain(result), "trace": driver.drain_events()}

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

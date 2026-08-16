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

* **Stub stdlib** — the `Pool` / `Map` host builtins required by
  docs/backend-ir.md, in-memory fakes that record every operation so demos
  and tests can assert ordering.
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Callable, Optional

__all__ = ["ConfigError", "ConfigSchema", "Frame", "Job", "Map", "Pool", "fmt", "set_trace"]


# ---------------------------------------------------------------------------
# tracing (test/demo observability for the stub stdlib)
# ---------------------------------------------------------------------------

_trace: Optional[Callable[[str], None]] = None


def set_trace(callback: Optional[Callable[[str], None]]) -> None:
    """Install a callback receiving one string per host-builtin operation."""
    global _trace
    _trace = callback


def _record(event: str) -> None:
    if _trace is not None:
        _trace(event)


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

    def install(self, body: Callable) -> Any:
        """Install the component body (a generator function) as one effect."""
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


class ConfigSchema:
    """Config fields as ``(name, type, default)`` triples; ``default=None``
    means required (IR ``"default": null``).

    Resolved inside the emitted ``apply`` rather than through the runtime's
    ``resolve_config``: cordis-py reads ``Config`` off dict plugins with
    ``getattr``, which never sees dict keys — see REPORT.md.
    """

    def __init__(self, fields: list) -> None:
        self.fields = [tuple(field) for field in fields]

    def resolve(self, config: Any) -> dict:
        value = dict(config or {})
        issues = []
        for name, type_name, default in self.fields:
            if name not in value:
                if default is None:
                    issues.append(f'missing required config field "{name}"')
                    continue
                value[name] = default
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


class Pool(_Closable):
    """In-memory stand-in for a connection pool; records every call."""

    _serial = 0

    def __init__(self, url: str, size: int) -> None:
        super().__init__()
        self.url = url
        self.size = size
        self.queries: list = []
        self.executed: list = []

    @classmethod
    def open(cls, url: str, size: int) -> "Pool":
        if isinstance(url, str) and url.startswith("boom://"):
            # deliberate test hook: a refusing acquisition, so suites can
            # exercise mid-body failure semantics (IR v1/A8, paper L-Raise)
            _record(f"pool.open refused {url}")
            raise RuntimeError(f"refused to open {url}")
        pool = cls(url, size)
        _record(f"{pool._tag}.open {url}")
        return pool

    def close(self) -> None:
        self._check_open("close")
        self.closed = True
        _record(f"{self._tag}.close {self.url}")

    def query(self, sql: str) -> list:
        self._check_open("query")
        self.queries.append(sql)
        _record(f"{self._tag}.query {sql}")
        return []

    def execute(self, sql: str) -> int:
        self._check_open("execute")
        self.executed.append(sql)
        _record(f"{self._tag}.execute {sql}")
        return 1


class Job:
    """Async host builtin (IR v1): `await Job.run(name)` resolves on a later
    tick and records the call, so `await` steps have something real to await."""

    runs: list = []

    @classmethod
    async def run(cls, name: str) -> str:
        _record(f"job.run {name} start")
        import asyncio

        for _ in range(5):
            await asyncio.sleep(0)
        cls.runs.append(name)
        _record(f"job.run {name} done")
        return name


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

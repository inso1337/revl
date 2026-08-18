"""Host-side helpers for the demos: bind the runtime, watch the trace.

Two of the "minor demo frictions" in docs/v2.0-roadmap.md live here.

**Parameterized runtime import.**  Emitted cordis-py modules open with
``from runtime import ConfigSchema, Frame, …``.  That text is part of the
frozen emitted output — it must stay byte-identical — so a demo cannot ask
the emitter for a different module name.  What it *can* do is decide what
the name ``runtime`` resolves to, because Python resolves an import against
``sys.modules`` before it ever touches ``sys.path``.  :func:`bind_runtime`
does exactly that, so a host can run emitted components against an
instrumented or vendored runtime without a `sys.path` ordering trick and
without any change to the generated source.

**Multi-observer tracing.**  :func:`watch` wraps ``runtime.add_trace`` in a
context manager, so a demo can add a second (or third) observer alongside
whatever the driver installed with ``set_trace`` and take it away again.
"""

from __future__ import annotations

import contextlib
import sys
import types
from typing import Callable, Iterator, List


def bind_runtime(module: types.ModuleType, name: str = "runtime") -> Callable[[], None]:
    """Make ``from <name> import …`` in emitted code resolve to ``module``.

    Returns a function that restores the previous binding.  Emitted output is
    untouched: this rebinds the *name*, not the generated import statement.
    """
    previous = sys.modules.get(name)

    sys.modules[name] = module

    def restore() -> None:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous

    return restore


@contextlib.contextmanager
def bound_runtime(module: types.ModuleType, name: str = "runtime") -> Iterator[types.ModuleType]:
    """:func:`bind_runtime` as a context manager."""
    restore = bind_runtime(module, name)
    try:
        yield module
    finally:
        restore()


@contextlib.contextmanager
def watch(runtime_mod: types.ModuleType, observer: Callable[[str], None]) -> Iterator[None]:
    """Subscribe an *additional* trace observer for the duration of the block.

    Independent of ``runtime.set_trace``: the driver's primary callback keeps
    running, and this observer survives a ``set_trace(None)`` teardown until
    the block exits.
    """
    unsubscribe = runtime_mod.add_trace(observer)
    try:
        yield
    finally:
        unsubscribe()


class HostMeter:
    """A second trace observer: counts host operations by kind, and keeps the
    resolved configuration of every component that reported one.

    It exists to be *another* subscriber — the demo's log is the first — so
    the log itself demonstrates that two observers now coexist.
    """

    def __init__(self) -> None:
        self.events: List[str] = []
        self.ops: dict = {}
        self.configs: dict = {}

    def __call__(self, event: str) -> None:
        self.events.append(event)
        head, _, rest = event.partition(" ")
        subject, _, op = head.rpartition(".")
        if op == "config" and subject:
            self.configs[subject] = rest
            return
        self.ops[op or head] = self.ops.get(op or head, 0) + 1

    def summary(self) -> str:
        if not self.ops:
            return "no host operations"
        return ", ".join(f"{op}×{n}" for op, n in sorted(self.ops.items()))

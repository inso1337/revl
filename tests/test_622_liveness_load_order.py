"""Issue #622 hardening — the production silence observer arms per-provider,
independent of activation TIMING.

`_LivenessMonitor` enrols every observable declared-ceiling provider from the IR
at load start, before `_load` drives activation. Load is sequential, so a
provider late in the load order is not plugged (has no fiber) until every earlier
provider has finished activating. Its silence clock, however, was started at load
start — so a slow EARLIER activation makes a LATER, independent provider look
silent past its own ceiling before it has begun activating at all.

The monitor must never charge (and then permanently drop) a provider whose
activation has not yet begun: a not-yet-plugged provider has no silence of its
own account. This regression test stands two independent ceiling-bearing
providers side by side, plugs only the first (still activating), advances the
clock past both ceilings, and asserts the second provider stays watched — its
timeout monitoring survives to time its OWN activation once it is reached.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.run import _Driver, _LivenessMonitor  # noqa: E402


# two INDEPENDENT providers, each with its own observable (host-only) ceiling
# and neither requiring the other. Load order is ProviderA then ProviderB.
TWO = """
service DbA { fn query(sql: Str) -> List[Row] }
service DbB { fn query(sql: Str) -> List[Row] }
component ProviderA provides a: DbA liveness 1s {
  let pa = effect Pool.open("a", 1) undo pa.close()
  provide a { fn query(sql) = pa.query(sql) }
}
component ProviderB provides b: DbB liveness 1s {
  let pb = effect Pool.open("b", 1) undo pb.close()
  provide b { fn query(sql) = pb.query(sql) }
}
"""


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _fake_driver(ir, *, fibers=None, resolved=()):
    drv = _Driver.__new__(_Driver)
    drv.ir = ir
    drv.generation = 1
    drv._seq = 0
    drv._events = []
    drv._observing = None
    drv._settled = []
    drv._monitor = None
    drv.FiberState = lambda s: types.SimpleNamespace(name=s)
    drv.fibers = fibers if fibers is not None else {}
    drv.resolved_keys = lambda: set(resolved)
    return drv


def test_a_later_providers_monitoring_survives_a_slow_earlier_activation():
    ir = compile_source(TWO, "two.rvl")
    drv = _fake_driver(ir)
    # the sequential load has reached ProviderA and is still driving its
    # activation: ProviderA is plugged and alive but SLOW — it keeps making
    # progress (fiber transitions), so it never breaches its own ceiling.
    # ProviderB is later in the order and has NOT been plugged yet (no fiber).
    drv.fibers = {"ProviderA": types.SimpleNamespace(state="PENDING")}
    clock = _Clock()
    mon = _LivenessMonitor(drv, clock=clock)
    assert mon.watching == {"ProviderA", "ProviderB"}

    # ProviderA's activation drags on for well over ProviderB's ceiling, but A
    # keeps signalling life (sub-ceiling steps), so A is never expired. The wall
    # clock still crosses B's 1s ceiling several times over.
    for _ in range(10):
        clock.advance(0.5)                       # A never silent a full 1s
        mon.note_transition("ProviderA", "PENDING")
        asyncio.run(mon.poll())

    # ProviderA (alive) is still watched; nothing about it was expired.
    assert "ProviderA" in mon.watching
    assert drv._events == []

    # ProviderB has not begun activating (no fiber) — it has no silence of its
    # own, so it must stay watched to time its OWN activation once the load
    # reaches it. The bug charges B's load-start clock and drops it here,
    # permanently, so its real activation is never monitored.
    assert "ProviderB" in mon.watching, (
        "a later provider's timeout monitoring was removed by a slow earlier "
        "activation and is never reinstated"
    )

    # once ProviderB is finally plugged and its activation begins, the monitor
    # times its OWN silence from that point — a full ceiling, not the leftover.
    drv.fibers["ProviderB"] = types.SimpleNamespace(state="PENDING")
    mon.note_transition("ProviderB", "PENDING")
    clock.advance(0.5)
    assert asyncio.run(mon.poll()) == []      # 0.5s < 1s ceiling: not silent yet
    assert "ProviderB" in mon.watching

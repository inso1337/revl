"""Routed-require lowering on the real cordis-py runtime (roadmap item 167).

A component that ``requires worker in realms("w1","w2","w3") strategy(...)`` and
``provides worker`` must, in its EMITTED body, fan each call out across the
worker realms and fail over when one withdraws — the emitter's realization of
what ``src/revl/run.py::_Router`` does in the py-tier driver. This is the
end-to-end proof that the emitted Router body itself routes (item 161 left the
routing in the driver; item 167 moves it into the emitted body).
"""

from __future__ import annotations

import pathlib
import sys

from cordis import Context
from cordis.fiber import FiberState

import emit
import runtime as runtime_mod
from conftest import flush, load_module

ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402


# Three worker realms behind one router (item 161's composition shape). Each
# worker isolates its provision into its own named realm (G2 per (key, realm));
# the RoundRobin router requires the key across all three and provides it once
# in the parent realm (G2 downstream). `strategy` is a parameter so the
# least_loaded path is exercised by the same scenario.
def _source(strategy: str | None) -> str:
    strat = f" strategy({strategy})" if strategy else ""
    return f"""
service Worker {{ fn call(request: Str) -> Str }}
component W1 provides worker: Worker {{
  isolate worker in realm("w1")
  provide worker {{ fn call(request) = "w1:" + request }}
}}
component W2 provides worker: Worker {{
  isolate worker in realm("w2")
  provide worker {{ fn call(request) = "w2:" + request }}
}}
component W3 provides worker: Worker {{
  isolate worker in realm("w3")
  provide worker {{ fn call(request) = "w3:" + request }}
}}
component Router requires worker: Worker provides worker: Worker {{
  isolate worker in realms("w1", "w2", "w3"){strat}
  provide worker {{ fn call(request) = worker.call(request) }}
}}
"""


def _boot(strategy: str | None = None):
    ir = compile_source(_source(strategy), "router.rvl")
    assert ir["ir_version"] == 2
    module = load_module(emit.emit(ir), "router_scenario")
    root = Context()
    fibers = {
        name: runtime_mod.plug(root, getattr(module, name))
        for name in ("W1", "W2", "W3", "Router")
    }
    return root, fibers


def _router_service(root):
    """The single `worker` provider a consumer resolves in the parent realm —
    exactly the routing proxy's provision (G2)."""
    return root.reflect.get("worker")


async def test_emitted_router_body_distributes_round_robin(trace):
    root, fibers = _boot("round_robin")
    await flush()
    for name, fiber in fibers.items():
        assert fiber.state is FiberState.ACTIVE, f"{name} is {fiber.state}"

    worker = _router_service(root)
    assert worker is not None, "router must provide `worker` in the parent realm"

    # six calls rotate w1,w2,w3,w1,w2,w3 in declaration order — the emitted body
    # itself is what fans out (no py driver in this test).
    got = [worker.call(str(i)) for i in range(6)]
    assert got == ["w1:0", "w2:1", "w3:2", "w1:3", "w2:4", "w3:5"]


async def test_emitted_router_body_fails_over_when_a_worker_withdraws(trace):
    root, fibers = _boot("round_robin")
    await flush()
    worker = _router_service(root)

    # prime the rotation, then withdraw w2's provider: its realm resolves to a
    # non-ACTIVE handle (reflect.get -> None) and drops out of the live set.
    assert worker.call("a") == "w1:a"
    fibers["W2"].dispose()
    await flush()
    assert fibers["W2"].state is not FiberState.ACTIVE

    # the next six calls skip w2 entirely and spread across the survivors,
    # re-resolved per call — reactive failover from the emitted body.
    got = [worker.call(str(i)) for i in range(6)]
    assert all(r.startswith(("w1:", "w3:")) for r in got), got
    assert not any(r.startswith("w2:") for r in got), got

    # a survivor is still the sole downstream provider (G2 holds through
    # failover): the consumer keeps resolving exactly this one proxy.
    assert _router_service(root) is worker


async def test_a_replacement_worker_re_enters_the_rotation(trace):
    root, fibers = _boot("round_robin")
    await flush()
    worker = _router_service(root)

    fibers["W2"].dispose()
    await flush()
    # re-provide worker in realm w2 (a replacement shard) — it re-enters on its
    # next turn, for free, off the same per-call re-resolution.
    ir = compile_source(_source("round_robin"), "router.rvl")
    module = load_module(emit.emit(ir), "router_scenario_repl")
    fibers["W2b"] = runtime_mod.plug(root, module.W2)
    await flush()

    got = [worker.call(str(i)) for i in range(6)]
    assert any(r.startswith("w2:") for r in got), got


async def test_all_withdrawn_raises_no_live_worker(trace):
    root, fibers = _boot("round_robin")
    await flush()
    worker = _router_service(root)
    for name in ("W1", "W2", "W3"):
        fibers[name].dispose()
    await flush()

    try:
        worker.call("x")
        assert False, "a fully-withdrawn pool must raise, not route"
    except RuntimeError as exc:
        assert "no live worker" in str(exc)


async def test_least_loaded_spreads_by_served_count(trace):
    root, fibers = _boot("least_loaded")
    await flush()
    worker = _router_service(root)

    # least_loaded always routes to the live realm served fewest so far; with
    # all three live and starting from zero it visits each in turn (ties broken
    # by declaration order via `min`), so three calls hit three distinct realms.
    got = {worker.call(str(i)) for i in range(3)}
    assert got == {"w1:0", "w2:1", "w3:2"}

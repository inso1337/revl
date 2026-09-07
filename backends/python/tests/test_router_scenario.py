"""Routed-require realization on the real cordis-py runtime (roadmap item 167).

A component that ``requires worker in realms("w1","w2","w3") strategy(...)`` and
``provides worker`` is realized as a ROUTING PROXY: post-G2 (#449/#110) a routed
key is not a plugged fiber and carries no ``provide worker {…}`` body — such a
body would be silently discarded, so the frontend now refuses it. The driver's
``_load`` sees the ``routes`` IR and installs a :class:`revl.run._Router` under
the key in the parent realm instead (``src/revl/run.py::_install_router``). That
proxy fans each call out across the worker realms and fails over reactively when
one withdraws.

This is the end-to-end proof, on the cordis-py backend, that the realized proxy
distributes, fails over, spreads by load and re-admits a replacement — the
routing behaviour of docs/distribution-model.md, exercised through the same
``_Driver`` seam ``revl run`` uses (item 161 left the routing in the driver;
item 162/167 record the ``routes`` IR the driver realizes here).
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
from revl.run import _Driver, _Router  # noqa: E402


# Three worker realms behind one router (item 161's composition shape). Each
# worker isolates its provision into its own named realm (G2 per (key, realm));
# the router requires the key across all three and provides it once in the
# parent realm (G2 downstream). It carries NO `provide worker {…}` body — a
# routed key is realized as a routing proxy, not a plugged fiber, so a body
# would be silently discarded and is refused (G2, #449/#110). `strategy` is a
# parameter so the least_loaded path is exercised by the same scenario.
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
}}
"""


async def _boot(strategy: str | None = None) -> _Driver:
    """Compile, emit and load the composition through the real `_Driver`, the
    same seam `revl run` boots with. `_load` plugs W1/W2/W3 as fibers in their
    named realms and, seeing the router's `routes` IR, realizes it as a `_Router`
    proxy provided under `worker` in the parent realm (`_install_router`)."""
    ir = compile_source(_source(strategy), "router.rvl")
    assert ir["ir_version"] == 2
    driver = _Driver(ir, {}, emit, runtime_mod, Context, FiberState)
    module = driver._emit_module(ir)
    await driver._load(ir, module)
    return driver


def _router_service(driver: _Driver):
    """The single `worker` provider a consumer resolves in the parent realm —
    exactly the routing proxy's provision (G2)."""
    return driver.root.get("worker")


async def test_emitted_router_body_distributes_round_robin(trace):
    driver = await _boot("round_robin")
    await flush()
    for name in ("W1", "W2", "W3"):
        fiber = driver.fibers[name]
        assert fiber.state is FiberState.ACTIVE, f"{name} is {fiber.state}"

    worker = _router_service(driver)
    assert isinstance(worker, _Router), "router must provide the routing proxy"

    # six calls rotate w1,w2,w3,w1,w2,w3 in declaration order — the driver's
    # realized proxy is what fans out across the worker realms.
    got = [worker.call(str(i)) for i in range(6)]
    assert got == ["w1:0", "w2:1", "w3:2", "w1:3", "w2:4", "w3:5"]


async def test_emitted_router_body_fails_over_when_a_worker_withdraws(trace):
    driver = await _boot("round_robin")
    worker = _router_service(driver)

    # prime the rotation, then withdraw W2: its realm resolves to a non-ACTIVE
    # handle (reflect.get -> None) and drops out of the live set.
    assert worker.call("a") == "w1:a"
    await driver._perform_withdrawal("W2")
    await flush()
    assert driver.fibers["W2"].state is not FiberState.ACTIVE

    # the next six calls skip w2 entirely and spread across the survivors,
    # re-resolved per call — reactive failover from the routing proxy.
    got = [worker.call(str(i)) for i in range(6)]
    assert all(r.startswith(("w1:", "w3:")) for r in got), got
    assert not any(r.startswith("w2:") for r in got), got

    # a survivor is still the sole downstream provider (G2 holds through
    # failover): the consumer keeps resolving exactly this one proxy.
    assert _router_service(driver) is worker


async def test_a_replacement_worker_re_enters_the_rotation(trace):
    driver = await _boot("round_robin")
    worker = _router_service(driver)

    await driver._perform_withdrawal("W2")
    await flush()
    # re-provide worker in realm w2 (a replacement shard) — it re-enters on its
    # next turn, for free, off the same per-call re-resolution. `plug` applies
    # the component's `isolate worker in realm("w2")` placement, so the fresh
    # provision lands in exactly the realm the proxy fans out to.
    ir = compile_source(_source("round_robin"), "router.rvl")
    module = load_module(emit.emit(ir), "router_scenario_repl")
    driver.fibers["W2b"] = runtime_mod.plug(driver.root, module.W2)
    await flush()

    got = [worker.call(str(i)) for i in range(6)]
    assert any(r.startswith("w2:") for r in got), got


async def test_all_withdrawn_raises_no_live_worker(trace):
    driver = await _boot("round_robin")
    worker = _router_service(driver)
    for name in ("W1", "W2", "W3"):
        await driver._perform_withdrawal(name)
    await flush()

    try:
        worker.call("x")
        assert False, "a fully-withdrawn pool must raise, not route"
    except RuntimeError as exc:
        assert "no live worker" in str(exc)


async def test_least_loaded_spreads_by_served_count(trace):
    driver = await _boot("least_loaded")
    worker = _router_service(driver)

    # least_loaded always routes to the live realm served fewest so far; with
    # all three live and starting from zero it visits each in turn (ties broken
    # by declaration order via `min`), so three calls hit three distinct realms.
    got = {worker.call(str(i)) for i in range(3)}
    assert got == {"w1:0", "w2:1", "w3:2"}

"""Multi-realm routing (item 173), EXECUTED on the cordis-wasm substrate.

item 167 landed emitted-body routing on py/ts/rust; wasm is revl's FIRST-PARTY
runtime, so item 173 builds the missing liveness primitive directly into the
substrate (`cordis-wasm/runtime.py`'s `route:<key>` host op) and wires the
emitter to consume it. This test proves BY RUNNING that a `Router` whose EMITTED
body routes fans a key out across three named worker realms and fails over when
one withdraws — the same four properties `tests/test_router_runtime.py` proves
on the py reference, on the real Python+wasmtime host:

  1. round-robin distributes calls across the live worker realms in order;
  2. a withdrawn worker realm drops out of the live set — its calls go to the
     survivors (reactive failover from the emitted body, no host-side Router);
  3. a re-plugged worker re-enters the rotation;
  4. G2 holds: downstream, exactly ONE provider of the bare key exists (the
     Router); the fan-out across `w1/worker`… is the router's private detail.

The runtime is the first-party cordis-wasm prototype. Point CORDIS_WASM at a
checkout (default: ~/Projects/cordis-wasm); without it (or without the wasmtime
Python package) these skip with a reason — never reported as passing.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

SCENARIO = (BACKEND / "scenarios" / "router.rvl").read_text()


def _emitter():
    spec = importlib.util.spec_from_file_location("revl_wasm_emit", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cordis_runtime():
    """Load the cordis-wasm runtime by explicit path or skip with a reason."""
    pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    root = os.environ.get("CORDIS_WASM") or str(Path.home() / "Projects" / "cordis-wasm")
    path = Path(root) / "runtime.py"
    if not path.exists():
        pytest.skip(f"cordis-wasm runtime not found at {path} (set CORDIS_WASM)")
    spec = importlib.util.spec_from_file_location("cordis_wasm_runtime", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"cordis-wasm runtime failed to import: {exc}")
    # item 173: the substrate must carry the routing primitive (ROUTE_NS) — an
    # older checkout predates it and cannot resolve a routed require.
    if not hasattr(module, "ROUTE_NS"):
        pytest.skip("cordis-wasm runtime predates the route:<key> primitive (item 173)")
    return module


def _wire():
    """Compile + emit the router scenario and plug it onto a live runtime.

    Returns (mod, rt, fibers) with the three workers and the Router ACTIVE.
    `fibers` maps component name -> its plugged Fiber, so a test can withdraw or
    re-plug one worker and re-observe routing.
    """
    mod = _cordis_runtime()
    ir = compile_source(SCENARIO)
    modules = _emitter().emit(ir)
    rt = mod.Runtime()
    fibers = {}
    for entry in ir["manifest"]["loadOrder"]:
        fibers[entry] = rt.plug(entry, modules[entry])
    return mod, rt, fibers, modules


def _call(rt, router, request):
    return rt.call(router, "provide:worker.call", request)


def test_round_robin_distributes_across_realms():
    """Property 1: successive calls rotate w1 -> w2 -> w3 -> w1 in order."""
    _mod, rt, fibers, _mods = _wire()
    router = fibers["Router"]
    assert rt.states()["Router"] == "active", rt.states()
    # request + 100/200/300 tags which realm served: w1=+100, w2=+200, w3=+300.
    results = [_call(rt, router, 5) for _ in range(6)]
    assert results == [105, 205, 305, 105, 205, 305], results


def test_withdrawn_worker_fails_over_to_survivors():
    """Property 2: unplug w2 — its calls skip to the survivors, no host Router."""
    _mod, rt, fibers, _mods = _wire()
    router = fibers["Router"]
    # w2's realm-scoped provision is live before withdrawal.
    assert "w2/worker" in rt.table
    rt.unplug(fibers["W2"])  # quiesces: w2/worker leaves the table
    assert "w2/worker" not in rt.table
    # six calls now rotate over the two survivors only (w1, w3), never 205.
    results = [_call(rt, router, 5) for _ in range(6)]
    assert 205 not in results, results
    assert set(results) == {105, 305}, results


def test_replugged_worker_reenters_rotation():
    """Property 3: re-plug w2 — it re-publishes w2/worker and re-enters."""
    mod, rt, fibers, modules = _wire()
    router = fibers["Router"]
    rt.unplug(fibers["W2"])
    assert "w2/worker" not in rt.table
    during = [_call(rt, router, 5) for _ in range(4)]
    assert 205 not in during, during
    rt.plug("W2", modules["W2"])  # a fresh W2 fiber, re-published
    assert "w2/worker" in rt.table
    after = [_call(rt, router, 5) for _ in range(6)]
    assert 205 in after, after


def test_g2_one_provider_of_the_bare_key_downstream():
    """Property 4: exactly one provider of the bare `worker` (the Router); the
    worker realms publish `w1/worker`… and NEVER a bare `worker`."""
    _mod, rt, fibers, _mods = _wire()
    router = fibers["Router"]
    assert "worker" in rt.table, sorted(rt.table)
    assert rt.table["worker"].fiber is router
    worker_keys = sorted(k for k in rt.table if k.endswith("worker"))
    assert worker_keys == ["w1/worker", "w2/worker", "w3/worker", "worker"], worker_keys


def test_all_workers_withdrawn_traps_no_silent_fallback():
    """No live worker is a trap (`unreachable`), never a silent parent-chain
    fallback to the Router providing the bare key — the exact gap the substrate
    primitive closes for go/java."""
    _mod, rt, fibers, _mods = _wire()
    router = fibers["Router"]
    for w in ("W1", "W2", "W3"):
        rt.unplug(fibers[w])
    assert not any(k.endswith("/worker") for k in rt.table)
    with pytest.raises(Exception):
        _call(rt, router, 5)

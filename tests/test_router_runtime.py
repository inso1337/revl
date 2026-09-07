"""Runtime routing — the stdlib Router / load-balancer (roadmap item 161).

Item 162 landed the *frontend*: `isolate <key> in realms("w1"…"wN")
strategy(...)` records a `routes` entry on the consumer's IR and verifies each
named realm has a provider. It did NOT route. This item is the runtime: the
py-tier reference driver (`src/revl/run.py`) resolves that `routes` IR into N
per-realm provider handles and forwards each call to a live worker by the
strategy, with reactive failover when one withdraws — realizing the pattern of
docs/distribution-model.md (item 163) and the stdlib Router of stdlib/router.rvl.

Two layers, two honesty rules:

* the selection logic (`_Router`) is pure — round-robin, least-loaded, and
  failover are proven against a fake registry on **every** interpreter, no
  runtime needed;
* the end-to-end proof (a real composition on the cordis-py driver: calls
  distribute, a withdrawn worker is skipped, consumers see ONE provider — G2 —
  and teardown leaves no residue) runs only under `@needs_cordis`; a
  runtime-less environment skips with a reason, never a feint at passing.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.run import NoLiveWorker, _Router  # noqa: E402

# Same availability gate test_run.py / test_replay.py use for real-cordis work.
try:  # noqa: SIM105
    import cordis  # noqa: F401
    HAVE_CORDIS = True
except ModuleNotFoundError:  # pragma: no cover — depends on the interpreter
    HAVE_CORDIS = False

needs_cordis = pytest.mark.skipif(
    not HAVE_CORDIS,
    reason="needs the cordis-py runtime (run under "
           "backends/python/.venv/bin/python)")


# --------------------------------------------------------------------------
# a fake registry so the selection logic is provable with no runtime
# --------------------------------------------------------------------------


class _FakeHandle:
    """A stand-in worker: `call(r)` tags the response with its realm, so a
    routing sequence is legible as the realms it visited."""

    def __init__(self, realm: str) -> None:
        self.realm = realm
        self.calls = 0

    def call(self, request: str) -> str:
        self.calls += 1
        return f"{self.realm}:{request}"


class _FakeReflect:
    def __init__(self, handle) -> None:
        self._handle = handle

    def get(self, _key: str):
        # a withdrawn realm resolves to None, exactly as cordis's strict
        # (ACTIVE-only) `reflect.get` does for a disposed provider.
        return self._handle


class _FakeScoped:
    def __init__(self, handle) -> None:
        self.reflect = _FakeReflect(handle)


class _FakeRoot:
    """`root.isolate(key, realm(w)).reflect.get(key)` over an in-memory
    realm→handle map. Set a realm's handle to None to model a withdrawal."""

    def __init__(self, handles: dict) -> None:
        self.handles = handles

    def isolate(self, _key: str, label):  # label is the realm string (below)
        return _FakeScoped(self.handles.get(label))


class _FakeRuntime:
    @staticmethod
    def realm_label(realm: str):
        # identity: the label IS the realm string, so _FakeRoot.isolate can key
        # the handle map by it.
        return realm


def _router(realms, strategy=None, live=None):
    """A `_Router` over fake handles; `live` (defaults to all) is the set of
    realms whose provider is currently ACTIVE."""
    live = set(realms if live is None else live)
    handles = {r: (_FakeHandle(r) if r in live else None) for r in realms}
    root = _FakeRoot(handles)
    router = _Router(root, _FakeRuntime, "worker", realms, strategy)
    return router, handles


# --------------------------------------------------------- round-robin


def test_round_robin_rotates_in_declaration_order():
    router, _ = _router(["w1", "w2", "w3"])
    seen = [router.call("k") for _ in range(6)]
    assert seen == ["w1:k", "w2:k", "w3:k", "w1:k", "w2:k", "w3:k"]


def test_round_robin_is_the_default_when_strategy_is_omitted():
    """`strategy` omitted (recorded as None) is the router's default —
    round-robin — not an error and not a silent single-target pin."""
    router, _ = _router(["w1", "w2"], strategy=None)
    assert [router.call("k") for _ in range(4)] == ["w1:k", "w2:k", "w1:k", "w2:k"]


def test_declaration_order_is_honored_not_sorted():
    router, _ = _router(["w3", "w1", "w2"])
    assert [router.call("k") for _ in range(3)] == ["w3:k", "w1:k", "w2:k"]


def test_single_realm_route_never_rotates():
    """`realms("w1")` is the degenerate route — one target, every call to it."""
    router, _ = _router(["w1"])
    assert [router.call("k") for _ in range(3)] == ["w1:k", "w1:k", "w1:k"]


# ---------------------------------------------------------------- failover


def test_a_withdrawn_worker_is_skipped_and_survivors_serve():
    """The core failover: a realm whose provider resolves to None (withdrawn)
    drops out of the rotation; the live realms keep serving, in order."""
    router, handles = _router(["w1", "w2", "w3"])
    assert [router.call("k") for _ in range(3)] == ["w1:k", "w2:k", "w3:k"]
    handles["w2"] = None  # W2 withdraws (peer-death → provider-withdrawal)
    router._root.handles["w2"] = None
    # cursor is back at w1 after the first sweep; w2 is skipped each time it
    # comes round, so the two survivors alternate.
    assert [router.call("k") for _ in range(4)] == ["w1:k", "w3:k", "w1:k", "w3:k"]


def test_a_replacement_reenters_the_rotation():
    """A realm that re-provides the key (a replacement worker) is live again on
    its next turn — nothing in the router pins to the dead handle."""
    router, _ = _router(["w1", "w2"], live={"w1"})
    assert [router.call("k") for _ in range(2)] == ["w1:k", "w1:k"]
    router._root.handles["w2"] = _FakeHandle("w2")  # replacement arrives
    assert [router.call("k") for _ in range(2)] == ["w2:k", "w1:k"]


def test_all_realms_withdrawn_raises_no_live_worker():
    router, _ = _router(["w1", "w2"], live=set())
    with pytest.raises(NoLiveWorker, match="no live worker"):
        router.call("k")


# ------------------------------------------------------------ least-loaded


def test_least_loaded_routes_to_the_fewest_served_live_worker():
    """least_loaded picks the live realm this router has served the fewest —
    so with all live it spreads evenly, and a fresh replacement (served 0) is
    preferred until it catches up."""
    router, _ = _router(["w1", "w2", "w3"], strategy="least_loaded")
    # ties break by declaration order (min is stable), so the first sweep is
    # w1, w2, w3 — each then has served-count 1.
    assert [router.call("k") for _ in range(3)] == ["w1:k", "w2:k", "w3:k"]
    # w2 withdraws; least-loaded now alternates the two survivors evenly.
    router._root.handles["w2"] = None
    assert sorted(router.call("k") for _ in range(4)) == [
        "w1:k", "w1:k", "w3:k", "w3:k"]


# --------------------------------------------------- traceable pass-through


def test_router_is_not_wrapped_as_a_traceable_service():
    """cordis probes a provided value for `__cordis_tracker__`; the proxy must
    refuse `_`-prefixed lookups so it is passed through raw (else the probe gets
    a bound method and the value is mis-wrapped)."""
    router, _ = _router(["w1"])
    assert getattr(router, "__cordis_tracker__", None) is None
    with pytest.raises(AttributeError):
        router._not_a_service_method  # noqa: B018


# ==========================================================================
# end-to-end on the real cordis-py driver
# ==========================================================================

APP = """
service Worker { fn call(r: Str) -> Str }
service Api { fn fetch(r: Str) -> Str }
component W1 provides worker: Worker { isolate worker in realm("w1") provide worker { fn call(r) = "w1:" + r } }
component W2 provides worker: Worker { isolate worker in realm("w2") provide worker { fn call(r) = "w2:" + r } }
component W3 provides worker: Worker { isolate worker in realm("w3") provide worker { fn call(r) = "w3:" + r } }
component RoundRobin requires worker: Worker provides worker: Worker {
  isolate worker in realms("w1", "w2", "w3") strategy(round_robin)
}
component Consumer requires worker: Worker provides api: Api {
  provide api { fn fetch(r) = worker.call(r) }
}
"""


def _build_driver(ir):
    """A `_Driver` on the real cordis-py backend, wired exactly as
    `run_command` wires it (the emitter + runtime imported off the backend
    path)."""
    from revl.run import _Driver  # noqa: PLC0415
    from revl._paths import backends_root  # noqa: PLC0415

    backend_dir = backends_root() / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import emit  # noqa: PLC0415
    import runtime as runtime_mod  # noqa: PLC0415
    from cordis import Context  # noqa: PLC0415
    from cordis.fiber import FiberState  # noqa: PLC0415

    return _Driver(ir, {}, emit, runtime_mod, Context, FiberState)


@needs_cordis
def test_end_to_end_round_robin_failover_and_g2_hold():
    ir = compile_source(APP, "app.rvl")

    async def scenario():
        driver = _build_driver(ir)
        module = driver._emit_module(ir)
        await driver._load(ir, module)

        # G2: the consumer resolves `worker` to exactly ONE provider — the
        # router proxy. The three workers are isolated in w1/w2/w3, invisible
        # in the parent realm; the pool is the router's private detail.
        worker = driver.root.get("worker")
        assert isinstance(worker, _Router)

        api = driver.root.get("api")
        # calls distribute round-robin across the three worker realms
        assert [api.fetch("k") for _ in range(6)] == [
            "w1:k", "w2:k", "w3:k", "w1:k", "w2:k", "w3:k"]

        # withdraw one worker: its route suspends, survivors keep serving
        await driver._perform_withdrawal("W2")
        after = [api.fetch("k") for _ in range(6)]
        assert after == ["w1:k", "w3:k", "w1:k", "w3:k", "w1:k", "w3:k"]
        assert "w2:k" not in after

        # G2 still holds after failover — one provider, still the router
        assert isinstance(driver.root.get("worker"), _Router)

        # teardown proves no residue: registry empty, provisions cleared,
        # disposables (incl. the routing provision) back to baseline
        await driver._teardown()
        assert driver.root.registry.size == 0
        assert driver.root.reflect.store == {}
        assert (driver.root.fiber._disposables.length
                == driver._baseline_disposables)

    asyncio.run(scenario())


@needs_cordis
def test_stdlib_router_template_compiles_and_boots():
    """stdlib/router.rvl — the canonical Router — boots on the driver: the
    three PoolWorkers come up in their realms and the RoundRobin router
    provides `worker` in the parent realm (one provider, G2), with no
    residue on teardown."""
    ir = compile_source(
        (ROOT / "stdlib" / "router.rvl").read_text(encoding="utf-8"),
        "router.rvl")

    async def scenario():
        driver = _build_driver(ir)
        module = driver._emit_module(ir)
        await driver._load(ir, module)
        worker = driver.root.get("worker")
        assert isinstance(worker, _Router)
        assert [worker.call("x") for _ in range(3)] == ["w1:x", "w2:x", "w3:x"]
        await driver._teardown()
        assert driver.root.reflect.store == {}

    asyncio.run(scenario())

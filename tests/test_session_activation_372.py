"""Roadmap item 372 — "loaded means loaded": a deferred component activation
must never leave a key listed LOADED while ROOT has no provider.

Cam's evidence: `ctx.plugin()` defers the component apply to the event loop via
a `_LazyTask` whose first statement is `await asyncio.sleep(0)`. On an
offloaded / closing-loop path (item 115's synchronous-`@py`-body `http_post`
pins the single loop, so dispatch is offloaded to a thread running `asyncio.run`
with a main-side timeout), the per-turn loop closed and SILENTLY CANCELLED the
activation. Because `str(CancelledError)` is the empty string there was NO
diagnostic, and the plugin was left listed as LOADED while `ROOT.get(key)`
stayed `None` forever — "loaded" was a lie.

These tests reproduce the failure through the runtime activation path and assert
the guarantee: a deferred activation now either (a) completes with the key
actually resolvable in ROOT and listed loaded, or (b) fails LOUDLY with a
diagnostic naming the key. The silent "loaded but None" state is unreachable.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the activation guarantee is a runtime property — needs cordis-py "
           "(`sh backends/python/setup.sh`, run under backends/python/.venv)",
)


def _backend():
    from revl.mcp.session import _backend as be  # noqa: PLC0415

    return be()


_IR = {
    "components": [{"name": "Deferred", "provides": {"widget": "W"},
                    "requires": {}, "config": []}],
    "manifest": {"loadOrder": ["Deferred"]},
    "services": {"W": {"methods": {"ping": {"params": []}}}},
}


def _deferred_module(gate, *, raise_in_body=False):
    """A component whose ASYNC activation body defers its `provide` past an
    event-loop await — exactly the `_LazyTask`/`async fn` shape Cam hit. The
    provide does not run until `gate` is set; if `raise_in_body` it raises after
    the gate instead of providing."""
    from runtime import Frame  # noqa: PLC0415 — backend path set by _backend()

    def _apply(ctx, config):
        frame = Frame(ctx, "Deferred")

        async def _body():
            await gate.wait()
            if raise_in_body:
                raise RuntimeError("activation blew up")
            yield ctx.provide("widget")
            ctx.set("widget", object())
            yield frame.drain

        frame.install(_body)

    module = types.ModuleType("revl_item372_mod")
    module.Deferred = {"name": "Deferred", "inject": [], "apply": _apply}
    return module


def _make_driver():
    emit, runtime_mod, Context, FiberState = _backend()
    from revl.run import _Driver  # noqa: PLC0415

    return _Driver(_IR, {}, emit, runtime_mod, Context, FiberState)


def _loaded_keys(driver) -> set:
    """The keys the driver reports as loaded/provided. The invariant under test:
    a key appears here IFF ROOT actually has a provider for it."""
    return {k for k in driver.resolved_keys()}


@needs_runtime
def test_closing_loop_never_leaves_a_false_loaded_key():
    """The per-turn loop closes underneath the still-deferred activation. The
    key must NOT be reported loaded while ROOT.get(key) is None."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        gate = asyncio.Event()  # never opened: activation blocks in its body
        driver = _make_driver()
        module = _deferred_module(gate)
        load_task = loop.create_task(driver._load(_IR, module))
        # let plug register the fiber and the activation reach its first await,
        # then the offloaded per-turn loop closes underneath it
        loop.run_until_complete(asyncio.sleep(0.05))
        load_task.cancel()
        try:
            loop.run_until_complete(load_task)
        except BaseException:  # noqa: BLE001 — the cancel is what we are modeling
            pass
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    # ROOT never got a provider — the deferred provide never ran
    assert driver.root.get("widget") is None
    # the guarantee: FIBERS/ROOT are consistent — the unresolved key is NOT
    # listed as loaded. (The bug listed it loaded while ROOT was None.)
    assert "widget" not in _loaded_keys(driver)


@needs_runtime
def test_deferred_activation_that_completes_is_loaded_for_real():
    """When the deferred activation DOES complete, the key is resolvable in ROOT
    and listed loaded — drive-to-completion, no regression."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        gate = asyncio.Event()
        gate.set()  # activation completes on the first turn
        driver = _make_driver()
        module = _deferred_module(gate)
        loop.run_until_complete(driver._load(_IR, module))
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    assert driver.root.get("widget") is not None
    assert "widget" in _loaded_keys(driver)


@needs_runtime
def test_activation_that_raises_fails_loudly_naming_the_key():
    """A deferred activation that RAISES must surface a real, typed diagnostic
    naming the component/key — never a silent empty-string CancelledError, and
    never a key left listed loaded."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        gate = asyncio.Event()
        gate.set()
        driver = _make_driver()
        module = _deferred_module(gate, raise_in_body=True)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011 — asserted below
            loop.run_until_complete(driver._load(_IR, module))
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    message = str(excinfo.value)
    assert message.strip(), "the failure must not be a silent empty string"
    assert "Deferred" in message or "widget" in message
    assert "widget" not in _loaded_keys(driver)

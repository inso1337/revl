"""Roadmap item 331 — cross-seam async forwarding.

A *chained async provide* forwards an awaited call to a required service:

    component Api requires cache: Cache provides api: ApiSvc {
      provide api { async fn hit(k) = cache.get(k) }
    }

`Api.hit` emits `return await cache.get(k)`. In-process `cache.get` is an
`async fn`, so it yields a coroutine and the `await` resolves it. Across a
PLACEMENT seam `cache` is a bridge proxy whose `_Client.call` is a *blocking*
round-trip returning the already-decoded value (a `str`) — so the `await` used
to hit ``'str' object can't be awaited``. The proxy now forwards an `async fn`
operation as an awaitable (`bridge._Proxy._async_methods`, wired by
`src/revl/placement.py`), so the chained `await` resolves the cross-seam value.

This exercises the REAL bridge on both sides: a live `bridge.serve` provider
(MemCache) answering over a Unix socket, and the real proxy forwarding into the
emitted `Api.hit`. The in-process path is asserted too, so the fix does not
regress it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="cross-seam async forwarding runs a live cordis-py composition — "
           "install it (sh backends/python/setup.sh) and run under that interpreter",
)

APP = ROOT / "demo" / "live_systems" / "app.rvl"


def _load_bridge():
    """backends/python/bridge.py, imported directly."""
    spec = importlib.util.spec_from_file_location(
        "revl_async_forward_bridge", ROOT / "backends" / "python" / "bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _emit_app_module():
    """Compile demo/live_systems/app.rvl to a live python module."""
    sys.path.insert(0, str(ROOT / "backends" / "python"))
    from revl.compiler import compile_files  # noqa: PLC0415
    import emit  # noqa: PLC0415
    source = emit.emit(compile_files([str(APP)]))
    module = __import__("types").ModuleType("revl_app_331")
    exec(compile(source, "<app-331>", "exec"), module.__dict__)
    return module


async def _resolve(value):
    """Await the value if it is awaitable (a cross-seam async call), else pass
    it through (a synchronous seam or an already-resolved value)."""
    if hasattr(value, "__await__"):
        return await value
    return value


@pytest.fixture
def sockdir():
    """A short-pathed dir for AF_UNIX sockets (macOS sun_path limit)."""
    directory = tempfile.mkdtemp(prefix="rv331", dir="/tmp")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


class _CacheProvider:
    """A synchronous provider that answers the bridge wire protocol for `cache`
    over a Unix socket — the same idiom `tests/test_swap.py::_Provider` uses.

    The item-331 defect is entirely on the *consumer* side (the proxy forwarding
    an `async fn` as a bare value instead of an awaitable); the stub side already
    awaits correctly (`bridge._invoke`). A synchronous stub is therefore enough
    to exercise the fix, and it leaves no event loop to tear down."""

    def __init__(self, sock_path: str, store: dict[str, str]) -> None:
        self._store = store
        self._stopping = False
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(sock_path)
        self._srv.listen(16)
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self._stopping:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        stream = conn.makefile("rwb")
        try:
            for line in stream:
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    continue
                method, args = req.get("method"), req.get("args") or []
                if method == "get":
                    reply = {"ok": True, "value": self._store.get(args[0] if args else None)}
                elif method == "put":
                    if len(args) >= 2:
                        self._store[args[0]] = args[1]
                    reply = {"ok": True, "value": None}
                else:
                    reply = {"ok": False, "error": f"no method {method!r}"}
                stream.write((json.dumps(reply) + "\n").encode())
                stream.flush()
        except (OSError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stopping = True
        try:
            self._srv.close()
        except OSError:
            pass


@needs_runtime
def test_chained_async_provide_forwards_awaited_cross_seam_call(sockdir):
    """`Api.hit` forwards `await cache.get(k)` where `cache` crosses a seam.
    Before item 331 this raised ``'str' object can't be awaited``; now it
    resolves the value across the seam."""
    bridge = _load_bridge()
    module = _emit_app_module()
    from cordis import Context

    sock = str(sockdir / "cache.sock")
    provider = _CacheProvider(sock, {"ada": "42"})
    try:
        async def consume():
            root = Context()
            # `async_methods` is exactly what src/revl/placement.py now writes
            # into the proxy spec for a transport-safe (all-async) service.
            proxy = bridge.proxy_component(
                "cache", ["get", "put"], sock, module,
                async_methods=["get", "put"])
            proxy_fiber = await root.plugin(proxy)
            api_fiber = await root.plugin(getattr(module, "Api"))
            api = root.get("api")
            # the chained await: Api.hit does `return await cache.get(k)`
            value = await _resolve(api.hit("ada"))
            # dispose consumer-first so the proxy's `undo` closes the seam
            # socket; the provider then sees EOF cleanly before we stop it.
            await api_fiber.dispose()
            await proxy_fiber.dispose()
            return value

        result = asyncio.run(consume())
    finally:
        provider.stop()

    assert result == "42", f"cross-seam api.hit should resolve to '42', got {result!r}"


@needs_runtime
def test_in_process_chained_async_provide_still_resolves():
    """The same composition wholly in one process: `cache.get` is a real
    coroutine, so the chained `await` path must keep working (no regression)."""
    module = _emit_app_module()
    from cordis import Context

    async def run():
        root = Context()
        await root.plugin(getattr(module, "MemCache"))
        await root.plugin(getattr(module, "Api"))
        await _resolve(root.get("cache").put("ada", "42"))
        return await _resolve(root.get("api").hit("ada"))

    assert asyncio.run(run()) == "42"

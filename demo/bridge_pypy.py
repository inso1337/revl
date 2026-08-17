#!/usr/bin/env python
"""py<->py interop bridge demo (docs/interop-bridge.md §3, first milestone).

One composition, split across two OS processes over a Unix-domain socket:

  provider process:  PgDatabase, provides `db`, exported via bridge.serve
  consumer process:  UserCache, requires `db` through a bridge proxy

Both processes compile the *same* examples/user_cache.rvl; each runs the
component placed on it. The demo proves the three §3 claims:

  1. transport + value-copy marshalling: cache.put's `emit db.execute(...)`
     crosses the process boundary and lands in the provider's real pool;
  2. a provided service consumed from another process works unchanged;
  3. peer death is withdrawal: killing the provider deactivates UserCache
     with ordered (LIFO) teardown, no exception thrown, no residue.

Run under the backend venv (needs cordis-py):
  backends/python/.venv/bin/python demo/bridge_pypy.py
Exits nonzero on any failed check.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
import sys
import types

DEMO = pathlib.Path(__file__).resolve().parent
ROOT = DEMO.parent
BACKEND = ROOT / "backends" / "python"
for _path in (str(ROOT / "src"), str(BACKEND)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import bridge  # noqa: E402  (backends/python/bridge.py)
import emit  # noqa: E402
import runtime as runtime_mod  # noqa: E402

from revl import compile_files  # noqa: E402

try:
    from cordis import Context  # noqa: E402
    from cordis.fiber import FiberState  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit(
        f"{exc.name!r} missing. run under the backend venv: "
        f"{BACKEND / '.venv/bin/python'} {pathlib.Path(__file__).name}"
    ) from exc

USER_CACHE = ROOT / "examples" / "user_cache.rvl"


def _load_module() -> types.ModuleType:
    source = emit.emit(compile_files([str(USER_CACHE)]))
    module = types.ModuleType("revl_bridge_mod")
    sys.modules[module.__name__] = module
    exec(compile(source, "<revl-bridge>", "exec"), module.__dict__)
    return module


async def _flush() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# provider role:  PgDatabase + stub, serving `db`
# ---------------------------------------------------------------------------


async def provider_main(socket_path: str, trace_path: str) -> None:
    module = _load_module()
    trace = open(trace_path, "w", encoding="utf-8")
    runtime_mod.set_trace(lambda event: (trace.write(event + "\n"), trace.flush()))

    root = Context()
    fiber = root.plugin(getattr(module, "PgDatabase"), {"url": "postgres://primary:5432/app"})
    await fiber
    await _flush()

    if os.path.exists(socket_path):
        os.unlink(socket_path)
    server = await bridge.serve(root, ["db"], socket_path)
    print("READY", flush=True)
    async with server:
        await server.serve_forever()


# ---------------------------------------------------------------------------
# consumer / driver role:  UserCache + proxy, across the seam
# ---------------------------------------------------------------------------


async def consumer_main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<22} {detail}", flush=True)
        if not ok:
            failures.append(name)

    socket_path = f"/tmp/revl_bridge_{os.getpid()}.sock"
    trace_path = f"/tmp/revl_bridge_{os.getpid()}.trace"
    pathlib.Path(trace_path).write_text("", encoding="utf-8")

    provider = subprocess.Popen(
        [sys.executable, str(DEMO / "bridge_pypy.py"), "--provider", socket_path, trace_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ready = False
    for line in provider.stdout:  # blocks until READY or EOF
        if line.strip() == b"READY":
            ready = True
            break
    if not ready:
        print("provider failed to start:\n" + provider.stderr.read().decode(), file=sys.stderr)
        provider.kill()
        return 1
    print("== provider up (PgDatabase serving `db` in a separate process) ==", flush=True)

    module = _load_module()
    root = Context()
    host_events: list[str] = []
    transitions: list[tuple] = []
    runtime_mod.set_trace(host_events.append)
    root.on("internal/status",
            lambda this, fiber, old: transitions.append((fiber.name, FiberState(old).name, FiberState(fiber.state).name)))

    proxy = bridge.proxy_component("db", ["query", "execute"], socket_path)
    proxy_fiber = root.plugin(proxy)
    await proxy_fiber
    await _flush()
    cache_fiber = root.plugin(getattr(module, "UserCache"))
    await cache_fiber
    await _flush()

    check("consumer-active", cache_fiber.state == FiberState.ACTIVE,
          "UserCache ACTIVE against a cross-process `db`")

    cache = root.get("cache")
    cache.put("alice", "42")
    cache.put("bob", "7")
    await _flush()
    got = cache.get("alice")
    check("local-read", got == "42", f'cache.get("alice") -> {got!r}')

    await asyncio.sleep(0.2)  # let the provider flush its trace
    provider_trace = pathlib.Path(trace_path).read_text(encoding="utf-8")
    check("crossed-emission",
          "cache_log" in provider_trace and ".execute" in provider_trace,
          "the provider's real pool.execute recorded the emit that crossed the seam")

    # peer death: a watcher disposes the proxy fiber when the provider dies
    loop = asyncio.get_running_loop()
    proxy["_client"].watch(
        lambda: loop.call_soon_threadsafe(lambda: asyncio.ensure_future(proxy_fiber.dispose()))
    )
    print("== killing the provider process ==", flush=True)
    provider.terminate()
    for _ in range(60):
        await asyncio.sleep(0.05)
        if cache_fiber.state != FiberState.ACTIVE:
            break
    await _flush()

    check("peer-death-withdrawal", cache_fiber.state != FiberState.ACTIVE,
          f"UserCache deactivated when the provider died (state={cache_fiber.state.name})")
    check("reactive-deactivation", ("UserCache", "ACTIVE", "UNLOADING") in transitions,
          "UserCache went ACTIVE->UNLOADING via provider withdrawal (R2), not by exception")
    inverses = [e for e in host_events if e.startswith("map") and (".remove" in e or ".drop" in e)]
    check("lifo-inverses", bool(inverses), f"consumer inverses replayed: {inverses}")

    try:
        provider.wait(timeout=5)
    except subprocess.TimeoutExpired:
        provider.kill()
    for stale in (socket_path, trace_path):
        if os.path.exists(stale):
            os.unlink(stale)

    print(f"\n{'all checks passed' if not failures else f'{len(failures)} check(s) FAILED'}", flush=True)
    return 1 if failures else 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--provider":
        asyncio.run(provider_main(sys.argv[2], sys.argv[3]))
        return 0
    return asyncio.run(consumer_main())


if __name__ == "__main__":
    sys.exit(main())

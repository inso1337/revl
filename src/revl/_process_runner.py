"""One process of a placement composition (spawned by src/revl/placement.py).

Reads a spec (argv[1] -> JSON) describing this process's slice of the
composition, and brings it up on a cordis-py Context:

  * load the keys it must consume from other processes as bridge proxies;
  * load its own components (in IR load order);
  * serve the keys it provides that other processes need;
  * run any probe expressions against its provided services;
  * hold until SIGTERM, then tear down (consumers first) and exit.

All output is line-prefixed with the process name so the conductor can
interleave several of these into one readable log. `[name] UP` marks a process
fully loaded; `[name] DOWN` marks a clean teardown.
"""

from __future__ import annotations

import asyncio
import builtins
import json
import signal
import sys
import types
from pathlib import Path


def _load_module(files: list[str]) -> types.ModuleType:
    from revl.compiler import compile_files  # noqa: PLC0415
    import emit  # noqa: PLC0415; backend dir already on sys.path

    source = emit.emit(compile_files(files))
    module = types.ModuleType("revl_proc_mod")
    sys.modules[module.__name__] = module
    exec(compile(source, "<revl-proc>", "exec"), module.__dict__)
    return module


async def _flush() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def run(spec: dict) -> None:
    name = spec["name"]
    backend_dir = str(Path(__file__).resolve().parents[2] / "backends" / "python")
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)

    import bridge  # noqa: PLC0415
    import runtime as runtime_mod  # noqa: PLC0415
    from cordis import Context  # noqa: PLC0415
    from cordis.fiber import FiberState  # noqa: PLC0415

    def log(channel: str, subject: str, detail: str = "") -> None:
        print(f"[{name}] {channel:<6}| {subject:<16}| {detail}".rstrip(), flush=True)

    runtime_mod.set_trace(lambda event: log("host", event.split(" ", 1)[0],
                                            event.split(" ", 1)[1] if " " in event else ""))

    module = _load_module(spec["files"])
    root = Context()
    root.on("internal/status", lambda _this, fiber, old:
            log("fiber", fiber.name, f"{FiberState(old).name} -> {FiberState(fiber.state).name}"))

    fibers: list[tuple[str, object]] = []

    # 1. proxies for keys provided by other processes
    for key, info in (spec.get("proxies") or {}).items():
        fiber = root.plugin(bridge.proxy_component(key, info["methods"], info["socket"]))
        await fiber
        await _flush()
        fibers.append((f"{key}-proxy", fiber))
        log("proxy", key, f"-> {info['socket']}")

    # 2. this process's own components, in IR load order
    for component in spec["components"]:
        config = (spec.get("config") or {}).get(component, {})
        fiber = root.plugin(getattr(module, component), config)
        await fiber
        await _flush()
        fibers.append((component, fiber))
        log("load", component, f"state={FiberState(fiber.state).name}")

    # 3. serve the keys other processes need
    server = None
    serve = spec.get("serve")
    if serve:
        server = await bridge.serve(root, serve["keys"], serve["socket"])
        log("serve", ", ".join(serve["keys"]), f"-> {serve['socket']}")

    # 4. probes: call provided services (may cross a seam), print results
    namespace = {key: root.get(key) for key in (spec.get("provides") or [])}
    for key in spec.get("proxies") or {}:
        namespace[key] = root.get(key)
    for expr in spec.get("probe") or []:
        try:
            value = eval(expr, {"__builtins__": builtins}, namespace)  # noqa: S307
            if hasattr(value, "__await__"):
                value = await value
            await _flush()
            log("probe", expr, f"=> {value!r}")
        except Exception as exc:  # noqa: BLE001
            log("probe", expr, f"ERROR {type(exc).__name__}: {exc}")

    print(f"[{name}] UP", flush=True)

    # 5. hold until the conductor stops us, then tear down consumers first
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover (non-unix)
            pass
    await stop.wait()

    for label, fiber in reversed(fibers):
        try:
            await fiber.dispose()
            await _flush()
        except Exception:  # noqa: BLE001; teardown is best-effort
            pass
    if server is not None:
        server.close()
    print(f"[{name}] DOWN", flush=True)


def main() -> None:
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    asyncio.run(run(spec))


if __name__ == "__main__":
    main()

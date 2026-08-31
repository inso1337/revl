"""One process of a placement composition (spawned by src/revl/placement.py).

Reads a spec (argv[1] -> JSON) describing this process's slice of the
composition, and brings it up on a cordis-py Context:

  * load the keys it must consume from other processes as bridge proxies;
  * load its own components (in IR load order);
  * serve the keys it provides that other processes need;
  * run any probe expressions against its provided services;
  * hold until SIGTERM, then tear down (consumers first) and exit.

While it holds, it also reads newline-delimited JSON *control* commands on
stdin — the channel `revl swap <component> --to <backend>` uses to drive a
live migration (docs/swap.md). Today one command is understood:

  {"op": "repoint", "key": "<k>", "socket": "<successor.sock>"}

which re-points the proxy for `<k>` from its current provider to a successor
serving at `<socket>` — a *planned cutover*, not the peer-death withdrawal a
provider vanishing would trigger (`bridge._Client.repoint`). The process
acknowledges with `[name] REPOINTED <k> -> <socket>`.

All output is line-prefixed with the process name so the conductor can
interleave several of these into one readable log. `[name] UP` marks a process
fully loaded; `[name] DOWN` marks a clean teardown, followed by a per-process
no-residue proof (`[name] residue ...`) so a provider torn down by a swap
proves it left nothing behind.
"""

from __future__ import annotations

import ast
import asyncio
import json
import signal
import sys
import threading
import types
from pathlib import Path

from ._paths import backends_root


def _eval_probe(expr: str, namespace: dict):
    """Evaluate one probe: `key.method(literal, ...)` — and nothing else.

    A placement file is *data*, not a program. Probes are therefore parsed and
    dispatched, never `eval`'d: the admitted grammar is exactly the one the
    rust backend already required (`placement.py::_parse_probe`) — one method
    call on one key this process holds, with literal arguments
    (`ast.literal_eval`). No builtins, no imports, no attribute chains, no
    expressions. Anything else is refused with a message naming the form.
    """
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"cannot parse probe (expected key.method(arg, ...)): {exc}") from exc
    call = tree.body
    if (not isinstance(call, ast.Call)
            or not isinstance(call.func, ast.Attribute)
            or not isinstance(call.func.value, ast.Name)
            or call.keywords):
        raise ValueError("probe must be of the form key.method(arg, ...)")
    key, method = call.func.value.id, call.func.attr
    if key not in namespace:
        held = ", ".join(sorted(namespace)) or "none"
        raise ValueError(f"{key!r} is not a key this process holds (holds: {held})")
    try:
        args = [ast.literal_eval(arg) for arg in call.args]
    except ValueError as exc:
        raise ValueError(f"probe arguments must be literals ({exc})") from exc
    target = getattr(namespace[key], method, None)
    if not callable(target):
        raise ValueError(f"{key!r} has no method {method!r}")
    return target(*args)


def _load_module(files: list[str]) -> types.ModuleType:
    from revl.compiler import compile_files  # noqa: PLC0415
    import emit  # noqa: PLC0415  backend dir already on sys.path

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
    backend_dir = str(backends_root() / "python")
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
    # key -> the proxy's _Client, so a `repoint` control command can carry the
    # seam to a successor without disposing the proxy fiber.
    clients: dict[str, object] = {}
    baseline_disposables = root.fiber._disposables.length

    # 1. proxies for keys provided by other processes. A proxy targets either a
    # local UDS (`socket`) or a network TCP+mTLS seam (`endpoint`); the bridge
    # normalizes both to an `Endpoint`. The deadline/withdrawal/canonical machinery
    # applies unchanged over either transport (docs/network-placement.md).
    for key, info in (spec.get("proxies") or {}).items():
        target = info.get("endpoint") or info["socket"]
        proxy = bridge.proxy_component(key, info["methods"], target, module,
                                       deadline=info.get("deadline"),
                                       deadlines=info.get("deadlines"),
                                       async_methods=info.get("async_methods"))
        clients[key] = proxy["_client"]
        fiber = root.plugin(proxy)
        await fiber
        await _flush()
        fibers.append((f"{key}-proxy", fiber))
        log("proxy", key, f"-> {bridge.Endpoint.from_spec(target).describe()}")

    # 2. this process's own components. G3 proves the dependency graph is a
    # checked DAG, so independent branches are provably independent and boot
    # concurrently; only real provider -> consumer edges serialize (§46,
    # docs/parallel-activation.md). `depends` carries those intra-process edges,
    # reconstructed by placement.py from the compiler's inject/provides
    # structure. Absent it (a spec written before §46), fall back to the old
    # strictly-sequential chain so nothing becomes *less* ordered.
    from revl.activation import (activate_concurrent,  # noqa: PLC0415
                                 sequential_prereqs, teardown_lifo)

    components = list(spec["components"])
    prereqs = spec.get("depends")
    if prereqs is None:
        prereqs = sequential_prereqs(components)

    async def _activate(component: str):
        config = (spec.get("config") or {}).get(component, {})
        fiber = root.plugin(getattr(module, component), config)
        await fiber
        await _flush()
        log("load", component, f"state={FiberState(fiber.state).name}")
        return fiber

    completion, errors = await activate_concurrent(components, prereqs, _activate)
    # completion is a valid topological order (a task finishes only after its
    # prereqs did); teardown reverses it, so consumers fall before providers —
    # LIFO within every chain, revert semantics preserved (G7).
    for component, fiber in completion:
        fibers.append((component, fiber))
    for component, exc in errors:
        log("load", component, f"ERROR {type(exc).__name__}: {exc}")

    # 3. serve the keys other processes need
    server = None
    serve = spec.get("serve")
    if serve:
        # `methods` (key -> declared operations) is the stub's allowlist; fall
        # back to the bare key list for a spec written before it existed. The
        # served endpoint is a UDS (`socket`) or a network TCP+mTLS seam
        # (`endpoint`); over TCP the provider demands the consumer's cert (mTLS).
        serve_target = serve.get("endpoint") or serve["socket"]
        server = await bridge.serve(root, serve.get("methods") or serve["keys"], serve_target,
                                    module=module)
        log("serve", ", ".join(serve["keys"]),
            f"-> {bridge.Endpoint.from_spec(serve_target).describe()}")

    # 4. probes: call provided services (may cross a seam), print results
    namespace = {key: root.get(key) for key in (spec.get("provides") or [])}
    for key in spec.get("proxies") or {}:
        namespace[key] = root.get(key)
    for expr in spec.get("probe") or []:
        try:
            value = _eval_probe(expr, namespace)
            if hasattr(value, "__await__"):
                value = await value
            await _flush()
            log("probe", expr, f"=> {value!r}")
        except Exception as exc:  # noqa: BLE001
            log("probe", expr, f"ERROR {type(exc).__name__}: {exc}")

    print(f"[{name}] UP", flush=True)

    # 5. hold until the conductor stops us, then tear down consumers first.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover (non-unix)
            pass

    # A control channel on stdin: the conductor pushes `repoint` commands here
    # to migrate a proxy to a successor provider (`revl swap`). Runs on a
    # daemon thread — `_Client.repoint` is thread-safe (its own lock) — and
    # hands results back to the loop only to log them in order.
    def control_reader() -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cmd.get("op") == "repoint":
                key, sock = cmd.get("key"), cmd.get("socket")
                client = clients.get(key)
                if client is None:
                    log("repoint", key or "?", "no such proxy in this process")
                    continue
                try:
                    client.repoint(sock)
                    print(f"[{name}] REPOINTED {key} -> {sock}", flush=True)
                except OSError as exc:
                    log("repoint", key, f"FAILED {type(exc).__name__}: {exc}")

    threading.Thread(target=control_reader, name="revl-control", daemon=True).start()

    await stop.wait()

    # LIFO teardown: `fibers` is [proxies..., own components in completion
    # order], and completion is a valid topological order, so reversing it tears
    # consumers down before providers within every chain (G7 revert semantics),
    # then releases the external proxies last. teardown_lifo centralizes that
    # reverse-order guarantee; disposal stays sequential (best-effort).
    async def _dispose(_label, fiber):
        try:
            await fiber.dispose()
            await _flush()
        except Exception:  # noqa: BLE001  teardown is best-effort
            pass

    await teardown_lifo(fibers, _dispose)
    if server is not None:
        server.close()

    # per-process no-residue proof: a provider torn down by a swap (or the
    # whole placement stopping) must leave its Context empty — the distributed
    # form of `revl run`'s residue report (src/revl/run.py::_teardown).
    checks = {
        "registry": root.registry.size == 0,
        "provisions": root.reflect.store == {},
        "effects": root.fiber._disposables.length == baseline_disposables,
    }
    detail = (f"registry={root.registry.size} provisions={sorted(root.reflect.store)} "
              f"disposables={root.fiber._disposables.length}/{baseline_disposables}")
    verdict = "no residue" if all(checks.values()) else "RESIDUE LEFT"
    print(f"[{name}] residue {verdict} | {detail}", flush=True)
    print(f"[{name}] DOWN", flush=True)


def main() -> None:
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    asyncio.run(run(spec))


if __name__ == "__main__":
    main()

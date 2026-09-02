"""One process of a placement composition (spawned by src/revl/placement.py).

Reads a spec (argv[1] -> JSON) describing this process's slice of the
composition, and brings it up on a cordis-py Context:

  * load the keys it must consume from other processes as bridge proxies —
    each same-composition proxy is first RE-ADMITTED against this process's
    own running manifest (item 337 Seam 2, `_boot_wiring_decision`) before it
    is wired; a proxy that fails admission is refused, never wired, and the
    refusal FAILS THE BOOT (`BootRefused`: no `UP`, non-zero exit) rather than
    leaving a half-dead composition behind;
  * load its own components (in IR load order);
  * serve the keys it provides that other processes need;
  * run any probe expressions against its provided services;
  * hold until SIGTERM, then tear down (consumers first) and exit.

While it holds, it also reads newline-delimited JSON *control* commands on
stdin — the channel `revl swap <component> --to <backend>` uses to drive a
live migration (docs/swap.md). Today one command is understood:

  {"op": "repoint", "key": "<k>", "socket": "<successor.sock>"}

  {"op": "repoint", "key": "<k>", "socket": "<successor.sock>",
   "component": "<c>", "backend": "<tier>"}

which re-points the proxy for `<k>` from its current provider to a successor
serving at `<socket>` — a *planned cutover*, not the peer-death withdrawal a
provider vanishing would trigger (`bridge._Client.repoint`). The `component`
and `backend` fields carry the admissible identity of the successor so this
process can RE-ADMIT it against its own running manifest before accepting the
cutover (item 337, `_repoint_decision`); a socket-only repoint with no such
reference is refused fail-closed. The process acknowledges an accepted repoint
with `[name] REPOINTED <k> -> <socket>`.

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


class BootRefused(RuntimeError):
    """This process REFUSED to wire one or more of its boot-time seams and can
    therefore not come up (item 337 Seam 2).

    A refusal at the REPOINT seam leaves a healthy composition running — the
    proxy keeps its current target, no blip — so a log line is the right
    weight there. A refusal at the BOOT seam has no healthy prior state to fall
    back on: the consumer's own components were compiled against a dependency
    that is now unwired, so every call into it fails at runtime with a
    `'<key>' has no method ...`. Continuing produced a half-dead composition
    that still printed `UP` and still exited 0, which is the failure mode that
    let a broken seam ship. So a refused boot proxy is a BOOT FAILURE: the
    process names every refused key, never prints `UP`, and exits non-zero, and
    the conductor's own `--once` gate ("processes did not come up") turns that
    into a non-zero `revl run --placement` exit.
    """


def _load_module(ir: dict) -> types.ModuleType:
    import emit  # noqa: PLC0415  backend dir already on sys.path

    source = emit.emit(ir)
    module = types.ModuleType("revl_proc_mod")
    sys.modules[module.__name__] = module
    exec(compile(source, "<revl-proc>", "exec"), module.__dict__)
    return module


def _repoint_decision(spec_files: list, running_ir: dict, cmd: dict) -> tuple[bool, str | None]:
    """Decide whether a `repoint` control command may be accepted, item 337.

    A `repoint` re-points a live proxy onto a successor provider. It must pass
    the SAME admission gate boot and `revl swap` use, so it can never substitute
    an un-admitted provider at a placement seam. The wire command therefore
    carries the successor's admissible identity (`component`, `backend`), and
    this process re-admits that component against its OWN running manifest via
    `placement.swap_admission` (the standalone admission gate, `placement.py`),
    which is exactly what the conductor ran and is what this process has in
    scope: every process spec carries the full composition source, so
    `running_ir = compile_files(spec["files"])` IS the running manifest, with no
    extra transport. (`gate.admit_into` admits a fresh source into a manifest,
    the add-a-component shape, not a tier re-point, so `swap_admission` is the
    fit here.) Returns (True, None) to accept, or (False, reason) to REFUSE.

    Fail-closed: a legacy socket-only command that carries no admissible
    reference is REFUSED, never silently accepted. This is a planned-cutover
    gate only; the peer-death withdrawal path (`bridge._Client`) is untouched.
    """
    component, backend = cmd.get("component"), cmd.get("backend")
    if not component or not backend:
        return False, ("repoint carries no admissible successor reference "
                       "(component/backend); refused fail-closed (item 337)")
    from revl.placement import swap_admission  # noqa: PLC0415
    try:
        _candidate, error = swap_admission(list(spec_files), running_ir, component, backend)
    except Exception as exc:  # noqa: BLE001  any admission failure fails closed
        return False, f"admission raised {type(exc).__name__}: {exc}"
    if error is not None:
        return False, error.splitlines()[0]
    return True, None


def _boot_wiring_decision(spec_files: list, running_ir: dict, info: dict) -> tuple[bool, str | None]:
    """Decide whether a boot-time bridge proxy may be wired to its provider,
    item 337 Seam 2 (`docs/design/337-polyglot-admission-mesh.md`).

    This is the landed repoint seam (`_repoint_decision`, above) moved one
    event earlier: instead of re-admitting a successor at a live cutover, the
    CONSUMER re-admits its provider before the INITIAL proxy wiring at boot.
    `info` is this process's own `spec["proxies"][key]` entry; placement.py
    already stamps the provider's admissible identity (`component`, `backend`)
    onto every same-composition proxy entry, so the selector is already in
    hand — no new transport. The gate runs against `running_ir =
    compile_files(spec["files"])`, exactly the manifest this process already
    computes at boot.

    THE QUESTION IS A RE-ADMISSION, NOT A SWAP. `_repoint_decision` asks
    `swap_admission`: "may this component be MOVED to that tier", which is
    right at a cutover, where a live consumer's in-address-space call site is
    about to become a cross-process one. At boot nothing moves: the provider is
    already on its tier, the seam already exists, and this consumer's call sites
    were compiled against that seam from the start. Asking the swap question
    here refused seams the conductor deliberately sanctioned — an
    address-space-bound (sync `fn`/`emission`) service across a same-tier
    py<->py seam is PERMITTED at plan time by construction, and across tiers it
    is permitted-and-reported — so a legitimate composition booted half-dead,
    its proxy silently unwired. `placement.seam_readmission` asks the plan-time
    question instead: recompile against the running manifest, then run the
    conductor's own `cross_tier_boundary_check` over the seam topology. Same
    gate, same verdict, independently re-derived by the receiver.

    Fail-closed, same three properties as `_repoint_decision`: a proxy entry
    carrying no admissible reference (component/backend) is REFUSED, never
    silently wired; any failure to reach a verdict fails closed; and a refused
    proxy is never wired at all. A cross-composition remote seam (item 151)
    never carries a component/backend — that is Seam 3, a different, deferred
    seam — so it is a caller's job to only invoke this for same-composition
    entries; it still fails closed here rather than assume-admit if one arrives
    without a selector.

    The honest limit (named in the design, not smuggled past it): at boot the
    consumer holds the SAME centrally-sliced source the conductor already
    compiled from, so this catches a race or an injected wiring (a proxy
    pointed somewhere the manifest does not sanction) and a tier-seam
    violation — it cannot catch a provider whose actual running bytes differ
    from the source this consumer holds.
    """
    component, backend = info.get("component"), info.get("backend")
    if not component or not backend:
        return False, ("boot proxy carries no admissible provider reference "
                       "(component/backend); refused fail-closed (item 337)")
    from revl.placement import seam_readmission  # noqa: PLC0415
    try:
        # `from_backend="py"` is this consumer's OWN tier — it is the py runner,
        # so the seam being judged is (py consumer <- `backend` provider),
        # which is the seam the conductor planned, not a hypothetical move.
        _candidate, error = seam_readmission(list(spec_files), running_ir,
                                             component, backend, from_backend="py")
    except Exception as exc:  # noqa: BLE001  any admission failure fails closed
        return False, f"admission raised {type(exc).__name__}: {exc}"
    if error is not None:
        return False, error.splitlines()[0]
    return True, None


async def _apply_boot_wiring(key: str, info: dict, spec_files: list, running_ir: dict,
                             wire, log=None) -> bool:
    """Decide, then (only if admitted) perform one boot-time proxy wiring —
    item 337 Seam 2. `wire` is the actual proxy setup (async: builds the bridge
    proxy and awaits its fiber, in `run()` below); it is invoked ONLY when the
    named provider passes `_boot_wiring_decision`. Returns True when the proxy
    was wired, False when it was REFUSED — in which case `wire` is never
    called at all, so the refusal actually blocks the wiring rather than
    merely producing a diagnostic. A remote seam (item 151: a genuinely
    separate composition, carrying no component/backend at all) is Seam 3, out
    of this seam's scope, and always wires. Split out of `run()`'s boot loop so
    the admission seam is unit-testable without a live Context, mirroring
    `_apply_repoint`.
    """
    if not info.get("remote"):
        ok, reason = _boot_wiring_decision(spec_files, running_ir, info)
        if not ok:
            if log:
                log("proxy", key, f"REFUSED (admission): {reason}")
            return False
    await wire()
    return True


def _apply_repoint(cmd: dict, clients: dict, spec_files: list, running_ir: dict,
                   log=None) -> bool:
    """Apply one `repoint` command: re-admit the successor against the running
    manifest, and ONLY on admission re-point the proxy's `_Client`. Returns True
    when the proxy was re-pointed, False when the command was refused (the proxy
    keeps serving its CURRENT target, no blip). Split out of the control loop so
    the admission seam is unit-testable (item 337)."""
    key, sock = cmd.get("key"), cmd.get("socket")
    client = clients.get(key)
    if client is None:
        if log:
            log("repoint", key or "?", "no such proxy in this process")
        return False
    ok, reason = _repoint_decision(spec_files, running_ir, cmd)
    if not ok:
        if log:
            log("repoint", key, f"REFUSED (admission): {reason}")
        return False
    try:
        client.repoint(sock)
        return True
    except OSError as exc:
        if log:
            log("repoint", key, f"FAILED {type(exc).__name__}: {exc}")
        return False


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

    from revl.compiler import compile_files  # noqa: PLC0415
    # The full composition IR this process compiles from `spec["files"]`. Every
    # process spec carries the whole composition source, so this is the RUNNING
    # manifest a re-pointed successor must re-admit against at the seam (item
    # 337, `_repoint_decision`); it also feeds the emitter for this slice.
    running_ir = compile_files(spec["files"])
    module = _load_module(running_ir)
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
    refused: list[str] = []
    for key, info in (spec.get("proxies") or {}).items():
        # item 337 Seam 2: before wiring a same-composition proxy, re-admit its
        # provider against OUR OWN running manifest (`_apply_boot_wiring` ->
        # `_boot_wiring_decision`), asking the plan-time seam question the
        # conductor answered rather than the relocation question `revl swap`
        # asks. A refused provider is never wired — `_wire` never runs — and
        # the refusal is FATAL to this process (`BootRefused` below): an unwired
        # dependency is a half-dead composition, not a survivable degradation.
        # A remote seam (item 151, a genuinely separate composition) is Seam 3,
        # out of scope here, and always wires.
        async def _wire(key=key, info=info) -> None:
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

        if not await _apply_boot_wiring(key, info, spec["files"], running_ir,
                                        _wire, log=log):
            refused.append(key)

    if refused:
        # Every refused seam is already logged with its reason; fail once,
        # naming them all, BEFORE loading anything. Nothing is up yet (the
        # proxy loop is the first boot phase), so there is nothing to tear down.
        raise BootRefused(
            f"[{name}] BOOT REFUSED: seam(s) {', '.join(refused)} failed "
            "re-admission at this consumer (item 337 Seam 2); a process cannot "
            "come up with an unwired dependency — see the REFUSED line(s) above")

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
                # item 337: a repoint must pass the SAME admission gate as boot
                # and `revl swap` before it can substitute a provider at this
                # seam. `_apply_repoint` re-admits the named successor against
                # our running manifest and re-points the `_Client` ONLY on
                # admission; on refusal the proxy keeps its current target (no
                # blip). This is the planned-cutover path only, distinct from
                # the peer-death withdrawal `bridge._Client` drives.
                if _apply_repoint(cmd, clients, spec["files"], running_ir, log=log):
                    print(f"[{name}] REPOINTED {cmd.get('key')} -> "
                          f"{cmd.get('socket')}", flush=True)

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
    try:
        asyncio.run(run(spec))
    except BootRefused as exc:
        # On stdout, so it lands in the conductor's interleaved trace next to
        # the REFUSED line that caused it — and exit non-zero, so a refused
        # seam is machine-visible instead of a note under a green run.
        print(str(exc), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

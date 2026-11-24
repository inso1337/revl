"""Single-process, once-mode runner for `revl run --backend wasm --once`
(docs/v2.0-roadmap.md §2, "Toward early production").

The wasm tier is the substrate tier: components are compiled to WAT modules
(backends/wasm/emit.py) and run on the cordis-wasm runtime, where the paradigm
is enforced by the wasmtime sandbox (the coeffect specification *is* the import
section; provisions *are* exports). That runtime lives in its own repo with its
own wasmtime-bearing venv, so — exactly like the rust tier boots a separate
cordis-rs process over the bridge seam — the wasm driver (src/revl/run_wasm.py)
boots this harness as a separate process under the cordis-wasm interpreter. The
driver does the revl-side work (compile + emit the WAT) and hands this harness a
spec of pre-emitted modules, so the harness needs only wasmtime + the runtime,
never the revl toolchain.

`--once` round-trip, mirroring the rust runner
(backends/rust/placement_runner/src/main.rs) and the py driver
(src/revl/run.py):

* plug every module in load order (providers first) on one cordis-wasm
  ``Runtime``; a consumer stays inactive until its coeffect is satisfied, so a
  provider-first order brings the whole mesh to ``ACTIVE``;
* print ``UP``;
* unplug LIFO (consumers before providers) — the compiled ``deactivate`` state
  machine replays each component's inverses;
* prove no residue: after teardown the live runtime must hold nothing — no
  fiber left in ``rt.fibers`` and no key left in ``rt.table`` (Σ). This is the
  substrate mirror of the py driver's ``registry.size==0`` / ``reflect.store=={}``
  check and the rust runner's ``registry().len()`` / ``reflect().services()``
  check: the tier proves the composition left nothing behind, not merely that
  unplug was called.

Usage (driven by run_wasm.py, not by hand):

    CORDIS_WASM=<dir> <cordis-wasm-venv-python> run_harness.py <spec.json>

Confinement (issue #3189): the spec carries the boundary policy's wasm
component limits (``fuel``, ``memoryBytes``, ``wallSeconds``). The harness
builds an ``Engine`` configured for fuel and epoch interruption, replaces the
one the runtime constructs, and installs a ``Store`` subclass that applies the
fuel budget, the linear-memory limiter and an epoch deadline to EVERY store the
runtime creates. A wall-clock ticker advances the engine epoch in proportion to
elapsed time, so a spinning guest traps at the ceiling instead of hanging. If
the installed runtime/wasmtime cannot apply a ceiling, the harness REFUSES to
run rather than running unconfined.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import time

#: The engine epoch advances one tick per this many seconds of wall time, so a
#: store's deadline can be expressed in wall-clock seconds. The ticker thread
#: may be scheduled late under a spinning guest; it catches up by incrementing
#: once per elapsed interval, which keeps the deadline near the wall clock.
_EPOCH_TICK_SECONDS = 0.05


class LimitsUnavailable(RuntimeError):
    """The runtime cannot apply the policy's wasm component ceilings."""


def _log(name: str, channel: str, subject: str, detail: str = "") -> None:
    print(f"[{name}] {channel:<6}| {subject:<16}| {detail}".rstrip(), flush=True)


def _limits_from_spec(spec: dict) -> tuple[int, int, float]:
    """The ceilings the driver resolved from the boundary policy.

    There is no unbounded mode: a spec with no limits, a partial set or a
    non-positive value is refused, because the alternative is running a
    generated component with no ceiling while the policy says it has one.
    """
    raw = spec.get("limits")
    if not isinstance(raw, dict):
        raise LimitsUnavailable(
            "the driver passed no component limits, and this harness never "
            "runs a composition unbounded")
    try:
        fuel = int(raw["fuel"])
        memory_bytes = int(raw["memoryBytes"])
        wall_seconds = float(raw["wallSeconds"])
    except (KeyError, TypeError, ValueError) as error:
        raise LimitsUnavailable(
            f"the component limits are not a usable fuel/memory/wall set: "
            f"{error}") from error
    if fuel <= 0 or memory_bytes <= 0 or wall_seconds <= 0:
        raise LimitsUnavailable(
            f"the component limits must all be positive, got fuel={fuel}, "
            f"memoryBytes={memory_bytes}, wallSeconds={wall_seconds}")
    return fuel, memory_bytes, wall_seconds


def _confined_engine():
    """An ``Engine`` that can carry fuel and epoch interruption, or a refusal."""
    import wasmtime  # noqa: PLC0415 - imported with the runtime, after the gate

    config = wasmtime.Config()
    # wasmtime's Config options are write-only `setter_property` descriptors:
    # reading them raises, so probe by SETTING and let the store-level check in
    # `_install_confinement` prove the engine really consumes fuel.
    for feature in ("consume_fuel", "epoch_interruption"):
        try:
            setattr(config, feature, True)
        except (AttributeError, TypeError) as error:
            raise LimitsUnavailable(
                f"this wasmtime cannot enable `{feature}`, so the fuel and "
                f"wall-clock ceilings cannot be applied") from error
    return wasmtime.Engine(config)


class _EpochTicker:
    """Advance the engine epoch in wall-clock proportion while the run lasts.

    A store created at epoch E with deadline D traps when the shared epoch
    reaches E + D. The ticker increments once per ``_EPOCH_TICK_SECONDS``, so a
    deadline expressed in ticks is a wall-clock ceiling for that component's
    guest code, including a tight ``(loop (br $l))`` that never yields.
    """

    def __init__(self, engine) -> None:
        self._engine = engine
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="revl-wasm-epoch", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.wait(0.01):
            now = time.monotonic()
            ticks = int((now - last) / _EPOCH_TICK_SECONDS)
            if not ticks:
                continue
            for _ in range(ticks):
                self._engine.increment_epoch()
            last += ticks * _EPOCH_TICK_SECONDS


def _install_confinement(runtime_module, engine, fuel: int,
                         memory_bytes: int, wall_seconds: float) -> None:
    """Make every store the runtime creates carry the component ceilings.

    The cordis-wasm runtime constructs its own ``Engine`` and one ``Store`` per
    fiber internally, and exposes no injection seam. The harness replaces the
    engine it already holds and the module's ``Store`` symbol for the duration
    of the boot, then verifies on a throwaway store that fuel was really set.
    If any piece is missing, ``LimitsUnavailable`` is raised BEFORE a module is
    plugged: refusing is honest, running unbounded is not.
    """
    import wasmtime  # noqa: PLC0415

    for method in ("set_fuel", "get_fuel", "set_limits", "set_epoch_deadline"):
        if not hasattr(wasmtime.Store, method):
            raise LimitsUnavailable(
                f"this wasmtime Store has no `{method}`; the policy's fuel, "
                f"memory and wall-clock ceilings cannot all be applied")
    deadline = max(1, round(wall_seconds / _EPOCH_TICK_SECONDS))
    original_store = runtime_module.Store

    class _ConfinedStore(original_store):
        def __init__(self, store_engine):
            super().__init__(store_engine)
            try:
                self.set_fuel(fuel)
                self.set_limits(memory_size=memory_bytes)
                self.set_epoch_deadline(deadline)
            except Exception as error:  # noqa: BLE001 - the runtime's own error
                raise LimitsUnavailable(
                    f"the runtime rejected the component ceilings: {error}"
                ) from error
            if self.get_fuel() != fuel:
                raise LimitsUnavailable(
                    "the runtime did not accept the fuel budget")

    runtime_module.Store = _ConfinedStore
    # verify the seam holds before any guest code is compiled or run
    try:
        runtime_module.Store(engine)
    except LimitsUnavailable:
        raise
    except Exception as error:  # noqa: BLE001
        raise LimitsUnavailable(
            f"the runtime cannot create a confined store: {error}") from error


def _read_wasm_str(memory, store, ptr: int) -> str:
    """Decode a canonical-ABI Str (`[u32 byte_len][utf8 bytes]`) at ``ptr`` from
    a plugged module's exported memory — the wasm tier's string layout
    (backends/wasm/emit.py). Kept revl-free (json + the runtime only), like the
    rest of this harness."""
    length = int.from_bytes(memory.read(store, ptr, ptr + 4), "little")
    return memory.read(store, ptr + 4, ptr + 4 + length).decode("utf-8")


def _install_wal_channel(rt, prefix: str = "[wal] ") -> None:
    """Bind the item 322 Slice 2 record channel: the host half of a record-mode
    module's ``coeffect:revl:wal.record`` import. At each witnessed transactional
    registration the module calls it with (seq, receiver_ptr, method_ptr,
    witness_ptr); this reads the three Str pointers back out of the calling
    fiber's memory and RELAYS one ``[wal] {…}`` frame per registration to stdout.
    The driver (:mod:`revl.run_wasm`) drains those frames into the durable host
    WAL — the wasm mirror of the go tier's direct ``revlRecordTransactional``
    fsync, split across the sandbox boundary the substrate enforces."""
    def record(fiber, seq, receiver_ptr, method_ptr, witness_ptr):
        memory = fiber.instance.exports(fiber.store)["memory"]
        store = fiber.store
        frame = {
            "seq": int(seq),
            "receiver": _read_wasm_str(memory, store, receiver_ptr),
            "method": _read_wasm_str(memory, store, method_ptr),
            "witness": _read_wasm_str(memory, store, witness_ptr),
        }
        # `prefix` carries the run's proof token when the runner sent one, so
        # the drain can tell this channel's frames from anything else that
        # reaches the shared stdout (issue #1621)
        print(prefix + json.dumps(frame), flush=True)

    rt.host_provide("revl:wal", {"record": record})


def _refuse_limits(error: Exception) -> int:
    """Refuse to run rather than run a composition the policy says is capped."""
    print(f"error: the cordis-wasm runtime cannot apply the policy's wasm "
          f"component limits: {error}; refusing to run the composition "
          f"unconfined", file=sys.stderr, flush=True)
    return 1


def main() -> int:
    spec = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    name = spec.get("name", "run")
    once = bool(spec.get("once", False))
    record = bool(spec.get("record", False))
    order = spec.get("order") or list((spec.get("modules") or {}).keys())
    modules = spec.get("modules") or {}
    # issue #3189: the ceilings the driver resolved from the boundary policy.
    # Read before any module is plugged, and a missing/unusable set refuses the
    # run: there is no unbounded mode.
    try:
        fuel, memory_bytes, wall_seconds = _limits_from_spec(spec)
    except LimitsUnavailable as error:
        return _refuse_limits(error)
    # issue #1621: the `--once` runner hands this process a per-run token as
    # the first line of stdin, and the four proof lines (UP, NO-RESIDUE,
    # RESIDUE-LEFT, DOWN) carry it, so a program's own output cannot forge
    # them. Read before any module is plugged, into a local of main(). The
    # spec carries only the flag: it is a file the program could read.
    # The same token tags the record channel's `[wal]` frames, which share
    # this stdout too: a forged descriptor would be replayed by recover.
    proof_name, wal_prefix = name, "[wal] "
    if spec.get("proofOnStdin"):
        token = sys.stdin.readline().strip()
        proof_name, wal_prefix = f"{name}#{token}", f"[wal#{token}] "

    cordis_wasm = os.environ.get("CORDIS_WASM") or str(pathlib.Path.home() / "Projects" / "cordis-wasm")
    sys.path.insert(0, cordis_wasm)
    from runtime import Runtime, State  # noqa: PLC0415 — cordis-wasm, wasmtime-backed

    # issue #3189: build the engine the runtime will use, configured for fuel
    # and epoch interruption, and start the wall-clock ticker BEFORE the first
    # store exists. Either step failing is a refusal.
    try:
        engine = _confined_engine()
    except LimitsUnavailable as error:
        return _refuse_limits(error)
    ticker = _EpochTicker(engine)
    ticker.start()
    try:
        rt = Runtime()
        # the runtime constructed a plain Engine in __init__; replace it before
        # any Module or Store is created, so everything below is confined.
        rt.engine = engine
        try:
            _install_confinement(sys.modules["runtime"], engine, fuel,
                                 memory_bytes, wall_seconds)
        except LimitsUnavailable as error:
            return _refuse_limits(error)
        # item 322 Slice 2: with record mode on, seed the durable-WAL channel
        # BEFORE plugging, so a record-mode module's `coeffect:revl:wal` import
        # resolves at activation and its framing calls relay while the mutation
        # registers.
        if record:
            _install_wal_channel(rt, wal_prefix)
        fibers = []
        for cname in order:
            fiber = rt.plug(cname, modules[cname])
            fibers.append((cname, fiber))
            _log(name, "load", cname, f"state={fiber.state.value}")

        # every fiber the composition placed must be ACTIVE for the mesh to be
        # up (a consumer left INACTIVE means its coeffect never resolved)
        all_active = all(f.state is State.ACTIVE for _, f in fibers)
        # the composition's own provisions — never the host-provided record
        # channel (item 322 Slice 2's `revl:wal`), which is the host's, not a
        # placed key.
        host_keys = getattr(rt, "host_keys", set())
        provided = sorted(k for k in rt.table if k not in host_keys)
        _log(name, "provide", "keys", ", ".join(provided) or "-")
        if not all_active:
            stuck = ", ".join(c for c, f in fibers if f.state is not State.ACTIVE)
            _log(name, "note", "inactive", stuck)
            # issue #3189: say WHICH ceiling stopped the component. The runtime
            # records the trap on the fiber; without this the operator sees only
            # PARTIAL and an exit code, with no sign the limits clause did it.
            for cname, fiber in fibers:
                error = getattr(fiber, "error", None)
                if fiber.state is not State.ACTIVE and error is not None:
                    detail = " ".join(str(error).split())
                    _log(name, "note", "raised", f"{cname}: {detail[:300]}")
        print(f"[{proof_name}] {'UP' if all_active else 'PARTIAL'}", flush=True)

        # teardown, consumers before providers (reverse load order)
        for cname, fiber in reversed(fibers):
            rt.unplug(fiber)
            _log(name, "swap", cname,
                 "dispose -> compiled inverses replay (LIFO)")
        # issue #3189: the runtime CONTAINS a trap in `deactivate`, so a wall or
        # fuel ceiling reached during teardown would otherwise be reported as a
        # clean unload even though the compiled inverses did not all replay.
        # The runtime records exactly that on its log; fail the round-trip.
        # (the marker is the cordis-wasm runtime's own log wording; if it ever
        # changes this check goes quiet rather than failing the run)
        teardown_interrupted = [
            line for line in getattr(rt, "log", [])
            if "trapped: contained" in line]
        if teardown_interrupted:
            _log(name, "note", "teardown",
                 "a ceiling trapped during teardown; the compiled inverses did "
                 "not all replay")

        if once:
            live_fibers = len(rt.fibers)
            # exclude host-provided provisions (item 322 Slice 2's `revl:wal`
            # record channel) from Σ residue — they are the host's, torn down
            # with the process, never a composition-left residue
            # (host_provide seeds them and `unplug` never withdraws them).
            host_keys = getattr(rt, "host_keys", set())
            live_services = sum(1 for k in rt.table if k not in host_keys)
            _log(name, "residue", "registry", f"{live_fibers} live plugin(s)")
            _log(name, "residue", "provisions",
                 f"{live_services} service(s) provided")
            if live_fibers == 0 and live_services == 0:
                print(f"[{proof_name}] NO-RESIDUE — the composition left nothing behind",
                      flush=True)
            else:
                print(f"[{proof_name}] RESIDUE-LEFT — see the residue lines above",
                      flush=True)

        print(f"[{proof_name}] DOWN", flush=True)
        return 0 if all_active and not teardown_interrupted else 1
    finally:
        ticker.stop()


if __name__ == "__main__":
    sys.exit(main())

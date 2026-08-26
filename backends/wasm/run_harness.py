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
"""

from __future__ import annotations

import json
import os
import pathlib
import sys


def _log(name: str, channel: str, subject: str, detail: str = "") -> None:
    print(f"[{name}] {channel:<6}| {subject:<16}| {detail}".rstrip(), flush=True)


def _read_wasm_str(memory, store, ptr: int) -> str:
    """Decode a canonical-ABI Str (`[u32 byte_len][utf8 bytes]`) at ``ptr`` from
    a plugged module's exported memory — the wasm tier's string layout
    (backends/wasm/emit.py). Kept revl-free (json + the runtime only), like the
    rest of this harness."""
    length = int.from_bytes(memory.read(store, ptr, ptr + 4), "little")
    return memory.read(store, ptr + 4, ptr + 4 + length).decode("utf-8")


def _install_wal_channel(rt) -> None:
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
        print("[wal] " + json.dumps(frame), flush=True)

    rt.host_provide("revl:wal", {"record": record})


def main() -> int:
    spec = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    name = spec.get("name", "run")
    once = bool(spec.get("once", False))
    record = bool(spec.get("record", False))
    order = spec.get("order") or list((spec.get("modules") or {}).keys())
    modules = spec.get("modules") or {}

    cordis_wasm = os.environ.get("CORDIS_WASM") or str(pathlib.Path.home() / "Projects" / "cordis-wasm")
    sys.path.insert(0, cordis_wasm)
    from runtime import Runtime, State  # noqa: PLC0415 — cordis-wasm, wasmtime-backed

    rt = Runtime()
    # item 322 Slice 2: with record mode on, seed the durable-WAL channel BEFORE
    # plugging, so a record-mode module's `coeffect:revl:wal` import resolves at
    # activation and its framing calls relay while the mutation registers.
    if record:
        _install_wal_channel(rt)
    fibers = []
    for cname in order:
        fiber = rt.plug(cname, modules[cname])
        fibers.append((cname, fiber))
        _log(name, "load", cname, f"state={fiber.state.value}")

    # every fiber the composition placed must be ACTIVE for the mesh to be up
    # (a consumer left INACTIVE means its coeffect never resolved)
    all_active = all(f.state is State.ACTIVE for _, f in fibers)
    # the composition's own provisions — never the host-provided record channel
    # (item 322 Slice 2's `revl:wal`), which is the host's, not a placed key.
    host_keys = getattr(rt, "host_keys", set())
    provided = sorted(k for k in rt.table if k not in host_keys)
    _log(name, "provide", "keys", ", ".join(provided) or "-")
    if not all_active:
        stuck = ", ".join(c for c, f in fibers if f.state is not State.ACTIVE)
        _log(name, "note", "inactive", stuck)
    print(f"[{name}] {'UP' if all_active else 'PARTIAL'}", flush=True)

    # teardown, consumers before providers (reverse load order)
    for cname, fiber in reversed(fibers):
        rt.unplug(fiber)
        _log(name, "swap", cname, "dispose -> compiled inverses replay (LIFO)")

    if once:
        live_fibers = len(rt.fibers)
        # exclude host-provided provisions (item 322 Slice 2's `revl:wal` record
        # channel) from Σ residue — they are the host's, torn down with the
        # process, never a composition-left residue (host_provide seeds them and
        # `unplug` never withdraws them).
        host_keys = getattr(rt, "host_keys", set())
        live_services = sum(1 for k in rt.table if k not in host_keys)
        _log(name, "residue", "registry", f"{live_fibers} live plugin(s)")
        _log(name, "residue", "provisions", f"{live_services} service(s) provided")
        if live_fibers == 0 and live_services == 0:
            print(f"[{name}] NO-RESIDUE — the composition left nothing behind", flush=True)
        else:
            print(f"[{name}] RESIDUE-LEFT — see the residue lines above", flush=True)

    print(f"[{name}] DOWN", flush=True)
    return 0 if all_active else 1


if __name__ == "__main__":
    sys.exit(main())

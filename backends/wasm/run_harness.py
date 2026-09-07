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


# --- the operator E-Stop, wasm tier (item 443 / issue #122) -----------------
#
# The wasm tier is single-process and REFUSED from placement (there is no wasm
# placement runner, bridge client, or stub — src/revl/placement.py refuses it
# with a redirect), so it has no conductor and no cross-process crossing seam.
# Its E-Stop is therefore a SINGLE-PROCESS seam, honored exactly where the py
# reference runtime honors it (backends/python/runtime.py::_estop_check, called
# from `plug`): an activation is a fresh batch of boundary crossings, so once an
# operator arms the latch this harness refuses to START a new one. It stops
# plugging, prints its in-flight inventory on one line, and dies where it stands
# with NO teardown — no LIFO unplug, no compiled inverses replayed, no
# no-residue proof, no DOWN — because an E-Stop is deliberately shaped to look
# like a crash to the recovery path (docs/design/443-estop.md). The harness
# stays revl-free (json + stdlib only), so this reader is a self-contained twin
# of `revl.estop.read_latch`, not an import of it.
_ESTOP_LATCH_ENV = "REVL_ESTOP_LATCH"
_ESTOP_EXIT = 75            # == _process_runner._ESTOP_EXIT
HALTED_LINE = "HALTED"      # == revl.estop.HALTED_LINE


def _estop_latch_path(spec: dict) -> str | None:
    """The latch to watch: the spec's ``estopLatch`` (what the driver threads
    through from ``revl run --estop-latch``), else the ambient env var, else
    none. Prefers the spec, matching how the py runner reads it."""
    explicit = spec.get("estopLatch")
    if isinstance(explicit, str) and explicit:
        return explicit
    return os.environ.get(_ESTOP_LATCH_ENV) or None


def _read_latch(path: str | None) -> dict | None:
    """The halt an operator armed at ``path``, or None when the latch is absent.
    A latch that exists but does not parse — or cannot be read — still reads as
    HALTED (fail closed); only a genuinely absent file reads as not-halted. Kept
    byte-for-byte in step with ``revl.estop.read_latch``."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
        return None
    except OSError:
        return _unreadable()
    except (ValueError, TypeError):
        return _unreadable()
    return record if isinstance(record, dict) else _unreadable()


def _unreadable() -> dict:
    return {"halted": True, "reason": "operator halt (unreadable latch)",
            "operator": "unknown"}


def _emit_halt(name: str, record: dict, loaded: list[str]) -> None:
    """Print ``[name] HALTED {json}`` — the in-flight inventory on one line, in
    the merged residue schema (docs/design/teardown-contract.md). A single
    process crosses no seam, so nothing is AMBIGUOUS (``inFlight`` is empty);
    every component already ACTIVE is STRANDED, because the halt runs none of
    their inverses."""
    inventory = {
        "process": name,
        "verdict": "halted",
        "reason": record.get("reason"),
        "operator": record.get("operator"),
        "activations": [],
        "inFlight": [],
        "stranded": [
            {"component": cname, "kind": "estop-stranded", "method": None,
             "outcome": "not-attempted", "seq": None}
            for cname in loaded
        ],
        "resumable": False,
    }
    print(f"[{name}] {HALTED_LINE} {json.dumps(inventory)}", flush=True)
    sys.stdout.flush()


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
    # item 443: the single-process E-Stop seam. `plug` is where this tier crosses
    # a boundary (an activation), so the latch is read once per plug and an armed
    # one refuses to START the next activation — the wasm mirror of the py
    # runtime's `_estop_check`. Unarmed (the default), `_read_latch` short-circuits
    # on a null path and this stats nothing, so an ordinary run is unchanged.
    latch = _estop_latch_path(spec)
    fibers = []
    for cname in order:
        halt = _read_latch(latch)
        if halt is not None:
            # Stop where we stand: name the ACTIVE components as stranded, print
            # the inventory, and die with NO teardown (os._exit runs no atexit,
            # no unplug, no residue proof, no DOWN — by design).
            _emit_halt(name, halt, [c for c, _ in fibers])
            os._exit(_ESTOP_EXIT)  # noqa: SLF001 — no teardown, by design
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

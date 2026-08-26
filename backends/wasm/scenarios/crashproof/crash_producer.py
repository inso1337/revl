"""The wasm half of the crash-recovery proof (roadmap item 322, Slice 2).

The wasm analog of backends/go/scenarios/crashproof/crash_proof_test.go: a REAL
process that boots a witnessed cordis-wasm composition, drives its durable WAL,
and either DIES mid-run or unloads clean. It is driven by the python half,
tests/test_wasm_crash_recovery.py, which sets the environment, runs it as a
subprocess, then reads the WAL back through `revl recover`. Run standalone with
no REVL_WAL it self-skips (exit 2).

What makes the residue durable on a tier with no direct filesystem: the Crasher
module is emitted in RECORD mode, so its witnessed transactional registration
frames the discharge-descriptor's runtime values out through the
`coeffect:revl:wal.record` host import. This producer binds that import to the
SAME host-side drain the once-mode driver uses (revl.run_wasm.wal_descriptor +
write_wal_record): it assembles the py-schema record and fsyncs it BEFORE the
call returns, so the inverse is re-issuable from the log alone after the process
dies.

Two modes, selected by env (set by the python driver):

  - REVL_CRASH_BEFORE_COMMIT=1 — os._exit the instant the mutation is durable,
    BEFORE commit. The WAL carries the descriptor but no `discharge` /
    `activation-complete`: recover rolls back.
  - unset — the clean control: unload LIFO (the compiled inverses discharge, the
    witnessed inverse never replays on a commit), then stamp `discharge` +
    `activation-complete` exactly as the go/py drivers do: recover rolls forward.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parents[3]
sys.path.insert(0, str(_ROOT / "src"))

from revl.run_wasm import (  # noqa: E402
    _emit_modules,
    wal_activation_complete,
    wal_descriptor,
    wal_discharge,
    wal_header,
    write_wal_record,
)


def _read_wasm_str(memory, store, ptr: int) -> str:
    """Decode a canonical-ABI Str (`[u32 byte_len][utf8 bytes]`) at ``ptr``."""
    length = int.from_bytes(memory.read(store, ptr, ptr + 4), "little")
    return memory.read(store, ptr + 4, ptr + 4 + length).decode("utf-8")


def main() -> int:
    wal_path = os.environ.get("REVL_WAL")
    if not wal_path:
        print("REVL_WAL not set: this producer runs under the python "
              "crash-recovery driver", file=sys.stderr)
        return 2

    cordis_wasm = os.environ.get("CORDIS_WASM") or str(
        pathlib.Path.home() / "Projects" / "cordis-wasm")
    sys.path.insert(0, cordis_wasm)
    from runtime import Runtime  # noqa: PLC0415 — cordis-wasm, wasmtime-backed

    ir = json.loads((_HERE / "crashproof.ir.json").read_text(encoding="utf-8"))
    # emit in RECORD mode: the Crasher module gains the `coeffect:revl:wal.record`
    # import and the framing call at its one witnessed transactional registration.
    module = _emit_modules(ir, record=True)["Crasher"]

    wal = open(wal_path, "w", encoding="utf-8")
    write_wal_record(wal, wal_header())

    drained: list[int] = []

    def record(fiber, seq, receiver_ptr, method_ptr, witness_ptr):
        # the host-side drain, inline: assemble the py-schema discharge-descriptor
        # from the module's framed runtime values and fsync it NOW — before this
        # call returns, so a crash immediately after leaves it durable.
        memory = fiber.instance.exports(fiber.store)["memory"]
        store = fiber.store
        receiver = _read_wasm_str(memory, store, receiver_ptr)
        method = _read_wasm_str(memory, store, method_ptr)
        witness = _read_wasm_str(memory, store, witness_ptr)
        write_wal_record(wal, wal_descriptor(int(seq), receiver, method, witness))
        drained.append(int(seq))

    rt = Runtime()
    rt.host_provide("revl:wal", {"record": record})

    fiber = rt.plug("Crasher", module)
    # the witnessed mutation ran in-process and its discharge-descriptor is now
    # durable on disk (write_wal_record fsynced it before `record` returned).

    if os.environ.get("REVL_CRASH_BEFORE_COMMIT") == "1":
        # abrupt death mid-session: no discharge, no terminal marker. The fsynced
        # descriptor is all that survives — exactly what recover must roll back.
        wal.flush()
        os._exit(137)

    # clean control: unload LIFO (the mutation commits, so its inverse discharges
    # rather than replaying), then stamp the commit-path proof + terminal marker.
    rt.unplug(fiber)
    write_wal_record(wal, wal_discharge(drained))
    write_wal_record(wal, wal_activation_complete())
    wal.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

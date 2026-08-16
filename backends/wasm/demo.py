"""revl on the substrate tier: compiled .rvl components running on the
cordis-wasm runtime (~/Projects/cordis-wasm), composing with a hand-written
WAT component in one mesh.

Run with the cordis-wasm venv (it has wasmtime):

    ~/Projects/cordis-wasm/.venv/bin/python backends/wasm/demo.py

What this proves, beyond the hosted backends:

- confinement is physical: the compiled components can only touch what
  their compiled import section names (G6 enforced by the instruction set);
- the emitted activate_step/deactivate state machine carries temporal
  composability with zero host bookkeeping for component-level inverses;
- the runtime is polyglot: Beacon and Auditor are compiled revl, the kv
  provider is hand-written WAT, and the runtime cannot tell.
"""

from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
CORDIS_WASM = pathlib.Path(os.environ.get("CORDIS_WASM", pathlib.Path.home() / "Projects" / "cordis-wasm"))
sys.path.insert(0, str(CORDIS_WASM))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "wasm"))

from runtime import Runtime, State  # noqa: E402  (cordis-wasm)
from revl import compile_files, compile_source  # noqa: E402

import emit  # noqa: E402  (this backend)

# The bottom of the mesh: a provider in hand-written WAT (from the
# cordis-wasm demo) — state in its own linear memory, dropped on unload.
KV_PROVIDER = r"""
(module
  (memory 1)
  (func (export "provide:kv.get") (param $k i32) (result i32)
    (i32.load (i32.mul (local.get $k) (i32.const 4))))
  (func (export "provide:kv.set") (param $k i32) (param $v i32)
    (i32.store (i32.mul (local.get $k) (i32.const 4)) (local.get $v))))
"""

BEACON_V2 = """
service Kv {
  fn get(k: Int) -> Int
  fn set(k: Int, v: Int)
}
service Status {
  fn shared() -> Int
}
component Beacon requires kv: Kv provides status: Status {
  effect kv.set(7, 111) undo kv.set(7, 0)
  effect kv.set(8, 222) undo kv.set(8, 0)
  provide status {
    fn shared() = kv.get(7)
  }
}
"""


def check(cond, message):
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {message}")
    assert cond, message


def main() -> None:
    ir = compile_files([str(ROOT / "examples" / "beacon.rvl")])
    modules = emit.emit(ir)
    rt = Runtime()

    def kv_at(fiber, key: int) -> int:
        return rt.call(fiber, "provide:kv.get", key)

    print("== 1. compiled consumer first: gated on its coeffect specification ==")
    auditor = rt.plug("Auditor", modules["Auditor"])
    check(auditor.state is State.INACTIVE, "Auditor waits — d = {kv, status} unsatisfied")

    print("== 2. polyglot provider arrives (hand-written WAT) ==")
    kv = rt.plug("kv", KV_PROVIDER)
    check(kv.state is State.ACTIVE, "kv provider active")
    check(auditor.state is State.INACTIVE, "Auditor still gated on status")

    print("== 3. compiled Beacon closes the chain ==")
    beacon = rt.plug("Beacon", modules["Beacon"])
    check(beacon.state is State.ACTIVE, "Beacon active (2-step state machine ran)")
    check(beacon.steps == 2, "activation took one iteration per effect step")
    check(auditor.state is State.ACTIVE, "Auditor auto-activated behind Beacon")
    check(kv_at(kv, 7) == 100 and kv_at(kv, 8) == 200, "Beacon's effects landed")
    check(kv_at(kv, 9) == 100, "Auditor snapshotted status.shared() into kv[9]")

    print("== 4. hot-swap Beacon from re-compiled source ==")
    modules_v2 = emit.emit(compile_source(BEACON_V2, "beacon-v2.rvl"))
    beacon2 = rt.swap(beacon, "Beacon-v2", modules_v2["Beacon"])
    check(beacon2.state is State.ACTIVE, "Beacon-v2 active")
    check(auditor.state is State.ACTIVE, "Auditor cycled onto the new provider")
    check(auditor.committed["status"] == beacon2.uid, "committed view moved to the new uid")
    check(kv_at(kv, 7) == 111 and kv_at(kv, 8) == 222, "new effects in place")
    check(kv_at(kv, 9) == 111, "Auditor re-snapshotted against Beacon-v2")

    print("== 5. withdraw Beacon-v2: guarded teardown, compiled inverses ==")
    rt.unplug(beacon2)
    check(auditor.state is State.INACTIVE, "Auditor deactivated (dependency withdrawn)")
    check(kv_at(kv, 9) == 0, "Auditor's compiled inverse ran (kv[9] reset)")
    check(kv_at(kv, 7) == 0 and kv_at(kv, 8) == 0,
          "Beacon-v2's compiled deactivate reverted its effects (LIFO)")
    order = [line for line in rt.log if line.startswith(("unload", "deactivate"))]
    check(order.index("unload Auditor") < order.index("unload Beacon-v2"),
          "dependent drained before its provider (the L-Unload guard)")

    print("\n-- full trace --")
    for line in rt.log:
        print("  " + line)


async def async_main() -> None:
    """A1 on the substrate: divert during an in-flight await (examples/pulse.rvl)."""
    import asyncio

    ir = compile_files([str(ROOT / "examples" / "pulse.rvl")])
    pulse_wat = emit.emit(ir)["Pulse"]

    print("\n== 6. A1: an await that completes normally ==")
    rt = Runtime()
    kv = rt.plug("kv", KV_PROVIDER)
    rt.plug("Pulse", pulse_wat)
    await rt.quiesce()
    check(rt.call(kv, "provide:kv.get", 1) == 11 and rt.call(kv, "provide:kv.get", 2) == 22,
          "both effects landed across the await")
    check(any("job Pulse: done 42" in line for line in rt.log), "the job ran to completion")

    print("== 7. A1: divert during the in-flight await skips later steps ==")
    rt = Runtime()
    kv = rt.plug("kv", KV_PROVIDER)
    pulse = rt.plug("Pulse", pulse_wat)
    for _ in range(50):
        await asyncio.sleep(0)
        if any("job Pulse: start" in line for line in rt.log):
            break
    else:
        raise AssertionError("Pulse never reached its await")
    rt.unplug(pulse)  # the world turns while the job is in flight
    await rt.quiesce()
    check(any(line.startswith("divert Pulse") for line in rt.log),
          "the divert fell at the boundary after the await landed")
    check(rt.call(kv, "provide:kv.get", 2) == 0, "the step after the await never executed")
    check(rt.call(kv, "provide:kv.get", 1) == 0, "the completed step's inverse ran")

    print("== 8. A1/A8: the async op refuses -> L-Raise, withheld ==")
    ir_bad = compile_source(BAD_PULSE, "bad-pulse.rvl")
    rt = Runtime()
    kv = rt.plug("kv", KV_PROVIDER)
    bad = rt.plug("BadPulse", emit.emit(ir_bad)["BadPulse"])
    await rt.quiesce()
    check(bad.failed, "the fiber landed Inactive(ξ) — withheld until explicit retry")
    check(rt.call(kv, "provide:kv.get", 1) == 0, "effects before the refusal reverted")

    print("\nall checks passed")


BAD_PULSE = """
service Kv {
  fn get(k: Int) -> Int
  fn set(k: Int, v: Int)
}
component BadPulse requires kv: Kv {
  effect kv.set(1, 11) undo kv.set(1, 0)
  await Job.run(13)
}
"""


if __name__ == "__main__":
    import asyncio

    main()
    asyncio.run(async_main())

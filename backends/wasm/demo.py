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
    print("\nall checks passed")


if __name__ == "__main__":
    main()

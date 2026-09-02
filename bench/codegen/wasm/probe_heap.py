"""Executed probe: the emitted bump heap is one fixed 64 KiB page, never
grows, and never frees.

Two facts, both run rather than argued:

1. A single canonical call with a `Str` argument larger than the page
   traps. The emitter writes `(memory (export "memory") 1)` and neither
   `$alloc` nor the exported `cabi_realloc` ever calls `memory.grow`, so
   the host's own write of the incoming string is already out of bounds.

2. Repeated calls exhaust the instance even when every argument fits:
   `$alloc` only bumps `$__hp`. This is measured on the CORE module (the
   component CLI re-instantiates per `--invoke`, which would hide it), by
   splicing in a driver export that replays the per-call allocation.

    PYTHONPATH=<repo>/src python bench/codegen/wasm/probe_heap.py
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import harness  # noqa: E402

#: Replays exactly what one canonical `Str` call does to the heap: lift the
#: argument into a fresh internal string, then bump an 8-byte return area.
DRIVER = """  (func (export "drive") (param $n i32) (param $len i32) (result i32)
    (local $i i32)
    (block $done
      (loop $go
        (br_if $done (i32.ge_u (local.get $i) (local.get $n)))
        (drop (call $__canon_lift_str (i32.const 0) (local.get $len)))
        (drop (call $alloc (i32.const 8)))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $go)))
    (global.get $__hp))
"""


def _core_wasm(wat: str, out: pathlib.Path) -> pathlib.Path:
    wat_path = out / "probe.wat"
    wasm_path = out / "probe.wasm"
    out.mkdir(parents=True, exist_ok=True)
    wat_path.write_text(wat, encoding="utf-8")
    subprocess.run(["wasm-tools", "parse", str(wat_path), "-o", str(wasm_path)],
                   check=True, capture_output=True, timeout=120)
    return wasm_path


def main() -> int:
    ok, why = harness.have_toolchain()
    if not ok:
        print(f"skipped: {why}")
        return 0

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="revl-wasm-heap-"))
    emitted = harness.emit(harness.PROGRAMS / "echo.revl", "Echoer")

    print("== 1. one call, argument larger than the single emitted page ==")
    comp = harness.build(emitted["core_wat"], emitted["wit"],
                         emitted["world"], tmp / "comp", "echo")
    for n in (16384, 32760, 40000, 65536):
        ok_call, _oof, out = harness.invoke(comp, 'echo("' + "a" * n + '")')
        verdict = "ok" if ok_call else "TRAP"
        note = ""
        if not ok_call:
            note = next((ln.strip() for ln in out.splitlines()
                         if "wasm trap" in ln or "memory fault" in ln), "")
        print(f"   arg {n:6d} B -> {verdict}  {note}")

    print("\n== 2. repeated calls, every argument well inside the page ==")
    wat = emitted["core_wat"].replace("\n  (func $echo ", "\n" + DRIVER + "  (func $echo ", 1)
    core = _core_wasm(wat, tmp / "core")
    wasmtime = harness.load_canonical().wasmtime_binary()
    for calls in (1, 8, 16, 32, 64, 128):
        res = subprocess.run(
            [wasmtime, "run", "--invoke", "drive", str(core),
             str(calls), "1024"],
            capture_output=True, text=True, timeout=120)
        if res.returncode == 0:
            print(f"   {calls:4d} calls x 1024 B -> heap at {res.stdout.strip()} B "
                  f"of 65536")
        else:
            line = next((ln.strip() for ln in
                         (res.stderr + res.stdout).splitlines()
                         if "wasm trap" in ln or "memory fault" in ln), "failed")
            print(f"   {calls:4d} calls x 1024 B -> TRAP  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

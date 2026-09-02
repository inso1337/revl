"""Executed probe: the emitted bump heap, its growth, and its lifetime.

Roadmap item 432(e) FIXED the growth half: `$alloc` now calls `$__heap_grow`
when the bump crosses the current memory limit, so both facts below now come
back green and the probe reads as a regression witness rather than a defect
report. The reclaim half is fixed too, and fact 3 is its proof: a canonical
export rewinds `$__hp` to the arena floor on the way out, so the heap
high-water mark no longer depends on the call count at all. The three facts
as they stood:

1. A single canonical call with a `Str` argument larger than the page
   fails. The emitter writes `(memory (export "memory") 1)` and neither
   `$alloc` nor the exported `cabi_realloc` ever called `memory.grow`, so
   the host's own placement of the incoming string ran off the end.

   Item 432(a) moved this line without removing it: the lift no longer
   makes a SECOND copy of the argument, so the largest string one call
   could carry roughly doubled. Bisected on the (a)/(c)/(f) base, 65520 B
   was the largest that worked and 65521 B failed, with wasmtime's own
   canonical check (`realloc return: beyond end of memory`) rather than
   the raw `memory fault at wasm address 0x10000` seen before (a).

2. Repeated calls exhaust the instance even when every argument fits:
   `$alloc` only bumps `$__hp`. This is measured on the CORE module (the
   component CLI re-instantiates per `--invoke`, which would hide it), by
   splicing in a driver export that replays the per-call allocation.
   Bisected on the same base: 64 calls with a 1 KiB string worked, 65
   trapped. (a)/(c)/(f) shrank what a call allocates, so this moved from
   63 calls to 65; it did not remove the cliff, because nothing frees.

3. Nothing was ever reclaimed, so growth turned "dies at call 65" into
   "grows without bound". Facts 1 and 2 both replay the per-call
   ALLOCATION rather than the call, which cannot see a rewind that lives
   in the export wrapper, so fact 3 drives the REAL canonical export the
   way a component host does -- one `cabi_realloc` for the argument, then
   the export -- and reports both where `$__hp` ended up and how far
   `memory.size` ever got. Before the reclaim fix: 1040 B after 1 call,
   66056 after 64, 67633160 B (1033 pages) after 65536, for a workload
   whose live set is one 1 KiB string. After it: the arena floor and one
   page, at every call count.

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

#: Replays exactly what one canonical `Str` call does to the heap, as the
#: emitter writes it TODAY: the host's `cabi_realloc` takes one buffer with
#: item 432(a)'s header headroom, and the lift then copies nothing into a
#: second one. Item 432(f) made the return area static, so there is no
#: per-call bump for it either. What is left is exactly one allocation per
#: call. Fact 3's driver, below, is the one that can see it being reclaimed.
DRIVER = """  (func (export "drive") (param $n i32) (param $len i32) (result i32)
    (local $i i32) (local $p i32)
    (block $done
      (loop $go
        (br_if $done (i32.ge_u (local.get $i) (local.get $n)))
        (local.set $p (i32.add
          (call $alloc (i32.add (local.get $len) (i32.const 8)))
          (i32.const 8)))
        (drop (call $__canon_lift_str (local.get $p) (local.get $len)))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $go)))
    (global.get $__hp))
"""


#: Fact 3. Drives the canonical export itself, so the per-call arena rewind in
#: the wrapper is inside what is measured. `drive_calls` reports where the bump
#: pointer ENDED; `drive_pages` reports how far `memory.size` ever got, which
#: only ever rises and is therefore the true high-water mark of the whole run.
CALL_DRIVER = """  (func $drive_calls (export "drive_calls")
      (param $n i32) (param $len i32) (result i32)
    (local $i i32) (local $p i32)
    (block $done
      (loop $go
        (br_if $done (i32.ge_u (local.get $i) (local.get $n)))
        (local.set $p (call $__cabi_realloc
          (i32.const 0) (i32.const 0) (i32.const 1) (local.get $len)))
        (drop (call $__export_echo (local.get $p) (local.get $len)))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $go)))
    (global.get $__hp))
  (func (export "drive_pages") (param $n i32) (param $len i32) (result i32)
    (drop (call $drive_calls (local.get $n) (local.get $len)))
    (memory.size))
"""

_ECHO_EXPORT = "revl:exported/echoer#echo"


def _with_call_driver(core_wat: str) -> str:
    """`core_wat` with `CALL_DRIVER` spliced in, and the two functions it calls
    given symbols (both are emitted as anonymous exported funcs)."""
    wat = core_wat.replace('(func (export "cabi_realloc")',
                           '(func $__cabi_realloc (export "cabi_realloc")', 1)
    wat = wat.replace(f'(func (export "{_ECHO_EXPORT}")',
                      f'(func $__export_echo (export "{_ECHO_EXPORT}")', 1)
    if "$__cabi_realloc" not in wat or "$__export_echo" not in wat:
        raise SystemExit("probe: the call-driver splice points moved")
    body = wat.rstrip()
    return body[:-1].rstrip("\n") + "\n" + CALL_DRIVER + ")\n"


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
    # 65520/65521 is the bisected cliff on the (a)/(c)/(f) base; 200000 is
    # well past any single-page story, so it can only pass by growing.
    for n in (16384, 32760, 40000, 65520, 65521, 200000):
        ok_call, _oof, out = harness.invoke(comp, 'echo("' + "a" * n + '")')
        verdict = "ok" if ok_call else "FAIL"
        note = ""
        if not ok_call:
            # `realloc return:` is how wasmtime reports an out-of-bounds
            # cabi_realloc, which is what this looks like once 432(a) has
            # removed the second copy that used to fault inside the module.
            note = next((ln.strip() for ln in out.splitlines()
                         if "wasm trap" in ln or "memory fault" in ln
                         or "realloc return" in ln), "")
        print(f"   arg {n:6d} B -> {verdict}  {note}")

    print("\n== 2. repeated calls, every argument well inside the page ==")
    wat = emitted["core_wat"].replace("\n  (func $echo ", "\n" + DRIVER + "  (func $echo ", 1)
    core = _core_wasm(wat, tmp / "core")
    wasmtime = harness.load_canonical().wasmtime_binary()
    for calls in (1, 8, 16, 32, 64, 65, 128, 4096):
        res = subprocess.run(
            [wasmtime, "run", "--invoke", "drive", str(core),
             str(calls), "1024"],
            capture_output=True, text=True, timeout=120)
        if res.returncode == 0:
            hp = int(res.stdout.strip())
            pages = -(-hp // 65536)
            print(f"   {calls:4d} calls x 1024 B -> heap at {hp} B "
                  f"({pages} page{'' if pages == 1 else 's'})")
        else:
            line = next((ln.strip() for ln in
                         (res.stderr + res.stdout).splitlines()
                         if "wasm trap" in ln or "memory fault" in ln), "failed")
            print(f"   {calls:4d} calls x 1024 B -> TRAP  {line}")

    print("\n== 3. repeated calls THROUGH the canonical export (the arena) ==")
    call_core = _core_wasm(_with_call_driver(emitted["core_wat"]),
                           tmp / "callcore")
    for calls in (1, 8, 64, 1024, 65536):
        hp = subprocess.run(
            [wasmtime, "run", "--invoke", "drive_calls", str(call_core),
             str(calls), "1024"],
            capture_output=True, text=True, timeout=600)
        pages = subprocess.run(
            [wasmtime, "run", "--invoke", "drive_pages", str(call_core),
             str(calls), "1024"],
            capture_output=True, text=True, timeout=600)
        if hp.returncode == 0 and pages.returncode == 0:
            print(f"   {calls:5d} calls x 1024 B -> $__hp {int(hp.stdout):9d} B, "
                  f"high-water {int(pages.stdout):4d} page"
                  f"{'' if int(pages.stdout) == 1 else 's'}")
        else:
            line = next((ln.strip() for ln in
                         (hp.stderr + hp.stdout + pages.stderr).splitlines()
                         if "wasm trap" in ln or "memory fault" in ln), "failed")
            print(f"   {calls:5d} calls x 1024 B -> TRAP  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
